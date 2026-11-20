"""Среда и оценщик в ОТДЕЛЬНОМ ПРОЦЕССЕ.

Агент общается со средой строками JSON через stdin/stdout дочернего процесса. Эталон никогда
не пересекает канал, кроме команд оценщика, защищённых токеном: токен знает только
оркестратор, запустивший процесс. Политика получает только наблюдения, а не объект клиента.
Канал — только ASCII (ensure_ascii), поэтому кодировка локали не важна.

Протокол (один JSON-объект на строку):
  {"cmd": "reset", "seed": int|null, "config": {...}|null, "options": {...}|null} -> {"ok": true, "obs", "info"}
  {"cmd": "step", "action": {...}}                -> {"ok": true, "obs", "reward", "terminated", "truncated", "info"}
  {"cmd": "grader_report", "token": str}          -> {"ok": true, "report": {...}}
  {"cmd": "oracle", "token": str}                 -> {"ok": true, "findings": {...}}
  {"cmd": "close"}
Внешний агент на любом языке может работать с этим протоколом напрямую:
  python -m auditgym.server --mode eval
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from .config import ConfigError
from .env import AuditEnv

TOKEN_ENV = "AUDITGYM_GRADER_TOKEN"


def serve(mode: str, time_limit_s: float) -> None:
    token = os.environ.pop(TOKEN_ENV, "")
    env = AuditEnv(mode=mode, time_limit_s=time_limit_s)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "reset":
                obs, info = env.reset(req.get("seed"), req.get("config"), options=req.get("options"))
                resp = {"ok": True, "obs": obs, "info": info}
            elif cmd == "step":
                obs, r, te, tr, info = env.step(req.get("action"))
                resp = {"ok": True, "obs": obs, "reward": r, "terminated": te, "truncated": tr, "info": info}
            elif cmd in ("grader_report", "oracle"):
                if not token or not hmac.compare_digest(str(req.get("token", "")), token):
                    resp = {"ok": False, "error": "forbidden"}
                elif cmd == "grader_report":
                    resp = {"ok": True, "report": env.grader_report()}
                else:
                    resp = {"ok": True, "findings": env.oracle_findings()}
            elif cmd == "close":
                break
            else:
                resp = {"ok": False, "error": "unknown command"}
        except ConfigError as e:
            resp = {"ok": False, "error": "config error: %s" % e}
        except Exception:  # внутренности (возможно, с состоянием оценщика) наружу не отдаём
            resp = {"ok": False, "error": "internal error"}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


class RemoteError(RuntimeError):
    pass


class SubprocessEnv:
    """Клиент на стороне агента: reset/step как у AuditEnv. Методы оценщика требуют токен,
    который хранится в этом объекте оркестратора, и в интерфейс политики не входят."""

    def __init__(self, mode: str = "eval", time_limit_s: float = 60.0):
        self._token = secrets.token_hex(16)
        root = str(Path(__file__).resolve().parent.parent)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update({"PYTHONPATH": root, TOKEN_ENV: self._token, "PYTHONIOENCODING": "utf-8"})
        self._p = subprocess.Popen(
            [sys.executable, "-m", "auditgym.server", "--mode", mode, "--time-limit", str(time_limit_s)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", env=env, cwd=root, bufsize=1)

    def _call(self, req):
        self._p.stdin.write(json.dumps(req) + "\n")
        self._p.stdin.flush()
        line = self._p.stdout.readline()
        if not line:
            raise RemoteError("процесс среды завершился")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise RemoteError(resp.get("error"))
        return resp

    def reset(self, seed=None, config=None, *, options=None):
        r = self._call({"cmd": "reset", "seed": seed, "config": config, "options": options})
        return r["obs"], r["info"]

    def step(self, action):
        r = self._call({"cmd": "step", "action": action})
        return r["obs"], r["reward"], r["terminated"], r["truncated"], r["info"]

    def grader_report(self):
        return self._call({"cmd": "grader_report", "token": self._token})["report"]

    def oracle_findings(self):
        return self._call({"cmd": "oracle", "token": self._token})["findings"]

    def close(self):
        if self._p.poll() is None:
            try:
                self._p.stdin.write(json.dumps({"cmd": "close"}) + "\n")
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

    ap = argparse.ArgumentParser(description="Среда и оценщик AuditGym в отдельном процессе (JSON lines)")
    ap.add_argument("--mode", default="eval", choices=["train", "eval"])
    ap.add_argument("--time-limit", type=float, default=60.0)
    a = ap.parse_args()
    serve(a.mode, a.time_limit)
