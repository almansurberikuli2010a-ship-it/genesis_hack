"""Среда и оценщик в ОТДЕЛЬНОМ ПРОЦЕССЕ.

Агент общается со средой строками JSON через stdin/stdout дочернего процесса. Эталон никогда
не пересекает канал, кроме команд оценщика, защищённых токеном: токен знает только
оркестратор, запустивший процесс. Политика получает только наблюдения, а не объект клиента.
Канал — только ASCII (ensure_ascii), поэтому кодировка локали не важна.

Протокол (один JSON-объект на строку, строка не длиннее MAX_REQUEST_CHARS символов):
  {"cmd": "reset", "seed": int|null, "config": {...}|null, "options": {...}|null, "token": str}
                                                  -> {"ok": true, "obs", "info"}
  {"cmd": "step", "action": {...}}                -> {"ok": true, "obs", "reward", "terminated", "truncated", "info"}
  {"cmd": "grader_report", "token": str}          -> {"ok": true, "report": {...}}
  {"cmd": "oracle", "token": str}                 -> {"ok": true, "findings": {...}}
  {"cmd": "close", "token": str}

Два режима запуска:
  python -m auditgym.server --mode eval
      открытый: внешний агент на любом языке сам ведёт эпизод — reset, step и close без токена;
      команды оценщика (grader_report, oracle) запрещены.
  python -m auditgym.server --mode eval --token-stdin
      оркестратор (так процесс запускает SubprocessEnv): первая строка stdin — токен оценщика.
      reset, grader_report, oracle и close требуют токен; без токена доступен только step.
      Токен не попадает ни в окружение, ни в аргументы процесса.

Строка, которую нельзя разобрать (неверный JSON, слишком глубокая вложенность, длиннее лимита),
с командой step верхнего уровня — неверное действие: ограниченная ошибка и израсходованный шаг.
Остальные такие строки — {"ok": false, "error": "invalid request"}.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from .config import ConfigError
from .env import AuditEnv

MAX_REQUEST_CHARS = 16 * 2 ** 20
_TOKEN_MAX = 256
_GUARDED = ("reset", "grader_report", "oracle", "close")  # в режиме оркестратора — только с токеном
_GRADER_ONLY = ("grader_report", "oracle")
_JSON_TOKEN = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[\[\]{}:,]')


def _read_line(stream, limit: int):
    """Строка не длиннее ``limit`` символов и признак «слишком длинная» (хвост такой строки
    дочитывается и отбрасывается, не занимая память)."""
    line = stream.readline(limit + 1)
    if len(line) <= limit or line.endswith("\n"):
        return line, False
    while True:
        rest = stream.readline(limit)
        if not rest or rest.endswith("\n"):
            return line, True


def _top_level_cmd(line: str):
    """Значение ключа "cmd" верхнего уровня в строке, которую json.loads не разобрал. Без рекурсии:
    считается только глубина скобок вне строк."""
    depth, key, pending = 0, None, None
    for m in _JSON_TOKEN.finditer(line):
        tok = m.group()
        if tok[0] == '"':
            if depth == 1 and pending == "cmd":
                try:
                    return json.loads(tok)
                except ValueError:
                    return None
            key, pending = (tok if depth == 1 else None), None
        elif tok == ":":
            try:
                pending = json.loads(key) if key is not None else None
            except ValueError:
                pending = None
            key = None
        else:
            depth += 1 if tok in "[{" else -1 if tok in "]}" else 0
            key = pending = None
    return None


def _step(env, action) -> dict:
    try:
        obs, r, te, tr, info = env.step(action)
    except Exception:
        # непредусмотренное исключение внутри шага: неверное действие, шаг израсходован (до reset
        # env.step(None) тоже бросит исключение — тогда ответ «internal error»)
        obs, r, te, tr, info = env.step(None)
    return {"ok": True, "obs": obs, "reward": r, "terminated": te, "truncated": tr, "info": info}


def handle(env: AuditEnv, line: str, token: str | None, too_long: bool = False):
    """Ответ на одну строку протокола; None — команда close. ``token`` — None в открытом режиме,
    строка (возможно, пустая — тогда закрыто всё, кроме step) в режиме оркестратора."""
    try:
        req = None if too_long else json.loads(line)
    except (ValueError, RecursionError, MemoryError):
        req = None
    if isinstance(req, dict):
        cmd = req.get("cmd")
    elif req is None and _top_level_cmd(line) == "step":
        cmd, req = "step", {}  # строку не разобрать, но это шаг: неверное действие
    else:
        return {"ok": False, "error": "invalid request"}
    if cmd in (_GUARDED if token is not None else _GRADER_ONLY):
        given = req.get("token")
        if not token or not isinstance(given, str) or not hmac.compare_digest(given.encode(), token.encode()):
            return {"ok": False, "error": "forbidden"}
    try:
        if cmd == "reset":
            obs, info = env.reset(req.get("seed"), req.get("config"), options=req.get("options"))
            return {"ok": True, "obs": obs, "info": info}
        if cmd == "step":
            return _step(env, req.get("action"))
        if cmd == "grader_report":
            return {"ok": True, "report": env.grader_report()}
        if cmd == "oracle":
            return {"ok": True, "findings": env.oracle_findings()}
        if cmd == "close":
            return None
        return {"ok": False, "error": "unknown command"}
    except ConfigError as e:
        return {"ok": False, "error": "config error: %s" % e}
    except Exception:  # внутренности (возможно, с состоянием оценщика) наружу не отдаём
        return {"ok": False, "error": "internal error"}


def serve(mode: str, time_limit_s: float, token_stdin: bool = False, stdin=None, stdout=None) -> None:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    token = stdin.readline(_TOKEN_MAX).strip() if token_stdin else None
    env = AuditEnv(mode=mode, time_limit_s=time_limit_s)
    try:
        while True:
            line, too_long = _read_line(stdin, MAX_REQUEST_CHARS)
            if not line:
                break
            resp = handle(env, line, token, too_long)
            if resp is None:
                break
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    finally:
        env.close()


class RemoteError(RuntimeError):
    pass


class SubprocessEnv:
    """Клиент на стороне оркестратора: reset/step как у AuditEnv. Токен оценщика хранится в этом
    объекте и передаётся процессу среды первой строкой stdin; без него процесс выполняет только step.
    Методы оценщика в интерфейс политики не входят."""

    def __init__(self, mode: str = "eval", time_limit_s: float = 60.0):
        self._token = secrets.token_hex(16)
        root = str(Path(__file__).resolve().parent.parent)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update({"PYTHONPATH": root, "PYTHONIOENCODING": "utf-8"})
        self._p = subprocess.Popen(
            [sys.executable, "-m", "auditgym.server", "--mode", mode, "--time-limit", str(time_limit_s), "--token-stdin"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", env=env, cwd=root, bufsize=1)
        try:
            self._p.stdin.write(self._token + "\n")
            self._p.stdin.flush()
        except OSError:  # процесс не запустился: ошибка будет у первой команды
            pass

    def _send(self, line: str):
        try:
            self._p.stdin.write(line + "\n")
            self._p.stdin.flush()
        except OSError:
            raise RemoteError("процесс среды завершился")
        line = self._p.stdout.readline()
        if not line:
            raise RemoteError("процесс среды завершился")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise RemoteError(resp.get("error"))
        return resp

    def _call(self, req):
        return self._send(json.dumps(req))

    def reset(self, seed=None, config=None, *, options=None):
        r = self._call({"cmd": "reset", "seed": seed, "config": config, "options": options, "token": self._token})
        return r["obs"], r["info"]

    def step(self, action):
        try:
            line = json.dumps({"cmd": "step", "action": action})
        except (TypeError, ValueError, RecursionError, MemoryError):  # не JSON: неверное действие, шаг израсходован
            line = json.dumps({"cmd": "step", "action": None})
        r = self._send(line)
        return r["obs"], r["reward"], r["terminated"], r["truncated"], r["info"]

    def grader_report(self):
        return self._call({"cmd": "grader_report", "token": self._token})["report"]

    def oracle_findings(self):
        return self._call({"cmd": "oracle", "token": self._token})["findings"]

    def close(self):
        if self._p.poll() is None:
            try:
                self._p.stdin.write(json.dumps({"cmd": "close", "token": self._token}) + "\n")
                self._p.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                self._p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._p.kill()
                self._p.wait()
        for f in (self._p.stdin, self._p.stdout):
            try:
                f.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


if __name__ == "__main__":
    import argparse

    from .landlock import set_non_dumpable

    # Linux: /proc/<pid>/{environ,fd,mem} процесса с эталоном и токеном закрыты для других процессов того
    # же пользователя (в том числе для политики без песочницы), ptrace к нему запрещён
    set_non_dumpable()
    ap = argparse.ArgumentParser(description="Среда и оценщик AuditGym в отдельном процессе (JSON lines)")
    ap.add_argument("--mode", default="eval", choices=["train", "eval"])
    ap.add_argument("--time-limit", type=float, default=60.0)
    ap.add_argument("--token-stdin", action="store_true",
                    help="режим оркестратора: первая строка stdin — токен; reset/grader_report/oracle/close только с ним")
    a = ap.parse_args()
    serve(a.mode, a.time_limit, token_stdin=a.token_stdin)
