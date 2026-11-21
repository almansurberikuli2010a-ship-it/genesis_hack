"""Адаптеры: подключение ЛЮБОЙ политики (в том числе политик организаторов).

Политика может быть
  * функцией ``fn(наблюдение: dict) -> действие: dict``, или
  * объектом с методами ``begin(первое_наблюдение)`` и ``act(наблюдение)``, или
  * именем базовой политики ("random", "reconciler", "flag_all", "oracle", "rule_auditor").

    from auditgym import run_policy
    result = run_policy(my_fn, seed=123, config={"preset": "interacting"}, mode="eval")
    result["grader"]["reward"], result["grader"]["f1"], result["trajectory"]

``run_policy`` (isolated=True) запускает среду и оценщика в одном отдельном процессе, а политику —
в другом, свежем: туда передаются сама политика (pickle) и наблюдения, обратно — только действия.
Seed и config эпизода, клиент среды с токеном оценщика и чтение итога эпизода остаются в процессе
оркестратора. Процесс политики работает в своей личной временной папке (cwd, TMPDIR); путей к
файлам эпизода он не получает, переменных окружения оркестратора AUDITGYM_* (например,
AUDITGYM_GRADER_LOG) — тоже: из них у него только AUDITGYM_POLICY_PROCESS, которую ставит run_policy.
Песочница процесса политики:
  * macOS — sandbox-exec: писать можно только в эту папку, читать файлы оценщика (truth.json,
    provenance.json, pre_injection/, grader/EP-*/) и каталог эпизода из config нельзя, посылать
    сигналы чужим процессам нельзя;
  * Linux — Landlock (auditgym/landlock.py, ядро ≥ 5.13, без root): белый список путей. Читать —
    только установку Python и системные библиотеки, пакеты и модули из sys.path (каталог из sys.path
    целиком не открывается: только подкаталоги-пакеты с __init__ и файлы модулей), /dev/null и
    /proc/self; писать — только в личную папку. Каталоги эпизодов (episodes/ проекта, episode_dir и
    episode_files.root из config) и найденные в разрешённых пакетах файлы оценщика недоступны вовсе,
    /proc других процессов и ptrace — тоже; с ABI 6 — и сигналы чужим процессам. Если Landlock
    недоступен (старое ядро, seccomp), политика работает без песочницы, а в результате
    (``policy_sandbox``) и журнале ``auditgym.policy`` записано, почему.
Что действовало в прогоне, видно в ``result["policy_sandbox"]``: "sandbox-exec", "landlock-abiN" или
"none: причина". Лимит 60 с соблюдается даже для зависшей политики; возвращаются траектория и отчёт
оценщика (доступный только после эпизода).

Ответ получает только oracle, которую оркестратор создаёт сам по явному запросу: строка "oracle" или
``BASELINE_ADAPTERS["oracle_grader_only"]`` (``make_policy("oracle")``). Она работает в процессе
оркестратора. Любой другой объект, в том числе ``Oracle()`` или её подкласс, уходит в процесс политики
и ответа не получает.
"""
from __future__ import annotations

import abc
import base64
import builtins
import importlib
import importlib.machinery
import io
import json
import logging
import marshal
import os
import pickle
import re
import shutil
import signal
import site
import subprocess
import sys
import sysconfig
import tempfile
import types

from . import landlock
from .env import AuditEnv
from .policies import Policy, is_grader_oracle, make_policy
from .runner import ROOT, run_episode
from .server import SubprocessEnv

POLICY_PROCESS_ENV = "AUDITGYM_POLICY_PROCESS"
_HEAPTYPE = 1 << 9  # Py_TPFLAGS_HEAPTYPE: класс описан на Python, а не встроен в интерпретатор
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_MAX_RESPONSE_CHARS = 16 * 2 ** 20  # строка ответа процесса политики
_MAX_NAME = 200
# файлы оценщика (export.write_episode: DIR/grader/<episode_id>/{truth.json, provenance.json, pre_injection/})
GRADER_PATH_PATTERNS = (r"/(truth|provenance)\.json$", r"/pre_injection(/|$)", r"/grader/EP-[^/]*(/|$)")


class FunctionPolicy(Policy):
    def __init__(self, fn, name="function_policy"):
        self.fn = fn
        self.name = name

    def act(self, obs):
        return self.fn(obs if obs.get("episode_id") else dict(obs, episode_id=self.episode_id))


def as_policy(p, name=None):
    if isinstance(p, Policy):
        return p
    if isinstance(p, str):
        return make_policy(p)
    if hasattr(p, "act") and hasattr(p, "begin"):
        return p
    if callable(p):
        return FunctionPolicy(p, name or getattr(p, "__name__", "function_policy"))
    raise TypeError("политика должна быть функцией, объектом Policy или именем базовой политики")


# ------------------------------------------------------------------ передача политики в её процесс

def _by_name(obj) -> bool:
    """Найдёт ли другой процесс функцию или класс по модулю и qualname. Нет — для __main__, вложенных,
    lambda и модулей, загруженных по пути к файлу без записи в sys.modules."""
    found = sys.modules.get(obj.__module__) if obj.__module__ != "__main__" else None
    for part in obj.__qualname__.split("."):
        found = getattr(found, part, None)
    return found is obj


def _new_function(code, n_cells):
    cells = tuple(types.CellType() for _ in range(n_cells))
    return types.FunctionType(code, {"__builtins__": builtins}, None, None, cells or None)


def _fill_function(f, state):
    f.__globals__.update(state["globals"])
    for cell, (full, value) in zip(f.__closure__ or (), state["cells"]):
        if full:
            cell.cell_contents = value
    for k in ("__defaults__", "__kwdefaults__", "__dict__", "__name__", "__qualname__", "__module__", "__doc__"):
        setattr(f, k, state[k])


def _new_class(meta, name, bases, ns):
    return meta(name, bases, ns)


def _fill_class(cls, state):
    attrs, qualname = state
    for k, v in attrs.items():
        setattr(cls, k, v)
    cls.__qualname__ = qualname
    abc.update_abstractmethods(cls)


class _Pickler(pickle.Pickler):
    """pickle, который передаёт по значению функции и классы, не находимые по имени в другом процессе."""

    def reducer_override(self, obj):
        if isinstance(obj, types.ModuleType):
            return importlib.import_module, (obj.__name__,)
        if isinstance(obj, types.CodeType):
            return marshal.loads, (marshal.dumps(obj),)
        if isinstance(obj, (staticmethod, classmethod)):
            return type(obj), (obj.__func__,)
        if isinstance(obj, property):
            return property, (obj.fget, obj.fset, obj.fdel, obj.__doc__)
        if isinstance(obj, types.FunctionType) and not _by_name(obj):
            names, codes = set(), [obj.__code__]
            while codes:
                c = codes.pop()
                names.update(c.co_names)
                codes += [k for k in c.co_consts if isinstance(k, types.CodeType)]
            cells = []
            for cell in obj.__closure__ or ():
                try:
                    cells.append((True, cell.cell_contents))
                except ValueError:  # пустая ячейка
                    cells.append((False, None))
            state = {k: getattr(obj, k) for k in ("__defaults__", "__kwdefaults__", "__dict__", "__name__",
                                                  "__qualname__", "__module__", "__doc__")}
            state.update(globals={k: obj.__globals__[k] for k in names if k in obj.__globals__}, cells=cells)
            return _new_function, (obj.__code__, len(cells)), state, None, None, _fill_function
        if isinstance(obj, type) and obj.__flags__ & _HEAPTYPE and not _by_name(obj):
            attrs = dict(vars(obj))
            slots = attrs.get("__slots__", ())
            for k in ("__dict__", "__weakref__", "_abc_impl", "__abstractmethods__") + (
                    (slots,) if isinstance(slots, str) else tuple(slots)):
                attrs.pop(k, None)
            ns = {"__slots__": slots} if "__slots__" in attrs else {}
            return _new_class, (type(obj), obj.__name__, obj.__bases__, ns), (attrs, obj.__qualname__), None, None, _fill_class
        return NotImplemented


def _sbpl(path: str) -> str:
    return '"%s"' % os.path.realpath(path).replace("\\", "\\\\").replace('"', '\\"')


def sandbox_profile(private_dir: str, forbidden=()) -> str:
    """Профиль sandbox-exec процесса политики (macOS). Запись — только в личную папку (и /dev/null,
    уже открытые stdout/stderr);
    чтение файлов оценщика запрещено (правило по каноническому пути на диске: регистр букв, ссылки
    и «..» не помогают; жёсткую ссылку создать нельзя — нет записи); сигналы — только своим процессам.
    Код среды и Python читаются всегда, даже если путь к ним совпал с шаблоном; каталоги ``forbidden``
    (каталог эпизода из config) не читаются вовсе."""
    code = sorted({str(ROOT / "auditgym"), sys.prefix, sys.base_prefix, sys.exec_prefix})
    rules = [
        "(version 1)", "(allow default)",
        "(deny file-write*)",
        "(allow file-write* (subpath %s))" % _sbpl(private_dir),
        '(allow file-write-data (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (literal "/dev/stdout") '
        '(literal "/dev/stderr") (regex #"^/dev/fd/[0-9]+$"))',
        "(deny file-read* %s)" % " ".join('(regex #"%s")' % p for p in GRADER_PATH_PATTERNS),
        "(allow file-read* %s)" % " ".join("(subpath %s)" % _sbpl(c) for c in code),
        "(deny signal)", "(allow signal (target same-sandbox))",
    ]
    if forbidden:
        rules.append("(deny file-read* %s)" % " ".join("(subpath %s)" % _sbpl(f) for f in forbidden))
    return " ".join(rules)


def _sandbox_prefix(private_dir: str, forbidden=()) -> list:
    """Начало команды процесса политики: sandbox-exec на macOS; на других системах — ничего (на Linux
    процесс ограничивает себя сам через Landlock, см. _enter_sandbox)."""
    if sys.platform != "darwin" or not os.path.exists(_SANDBOX_EXEC):
        return []
    return [_SANDBOX_EXEC, "-p", sandbox_profile(private_dir, forbidden)]


def policy_sandbox() -> str:
    """Чем ограничен процесс политики на этой системе: "sandbox-exec" (macOS), "landlock" (Linux с
    Landlock) или "process" (только отдельный процесс)."""
    if sys.platform == "darwin" and os.path.exists(_SANDBOX_EXEC):
        return "sandbox-exec"
    return "landlock" if landlock.abi()[0] else "process"


# ------------------------------------------------------------------ Landlock (Linux): белый список путей

# системные библиотеки (динамический загрузчик, libffi, libssl…) и запуск python потомками политики
_LL_SYSTEM_RX = ("/lib", "/lib32", "/lib64", "/libx32", "/usr/lib", "/usr/lib32", "/usr/lib64", "/usr/libx32",
                 "/usr/local/lib")
_LL_SYSTEM_R = ("/usr/share/zoneinfo", "/etc/ld.so.cache", "/etc/localtime", "/etc/passwd", "/etc/group",
                "/etc/nsswitch.conf")
_LL_DEVICES = ("/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom")
_GRADER_RES = tuple(re.compile(p) for p in GRADER_PATH_PATTERNS)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _listdir(path: str) -> list:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _library_dirs() -> set:
    """Каталоги установки Python: стандартная библиотека и site-packages (открываются целиком)."""
    out = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            out.add(sysconfig.get_path(key))
        except KeyError:
            pass
    try:
        out.update(site.getsitepackages())
    except AttributeError:  # старый virtualenv
        pass
    if site.ENABLE_USER_SITE:
        out.add(site.getusersitepackages())
    return {os.path.realpath(p) for p in out if p}


class _LandlockPlan:
    """Сборка правил: дерево открывается одним правилом, только если внутри нет запрещённых путей и
    каталогов кода (они открываются отдельно); иначе — по детям, а сам каталог попадает в prefill:
    кэш импорта для него заполняется до ограничения (перечислить его содержимое потом будет нельзя)."""

    def __init__(self, forbidden, code):
        self.forbidden = {os.path.realpath(f) for f in forbidden}
        self.code = {os.path.realpath(c) for c in code}
        self.rules, self.prefill = [], []

    def blocked(self, real: str) -> bool:
        return any(_within(real, f) for f in self.forbidden)

    def scan(self, top: str) -> None:
        """Каталоги с файлами оценщика внутри разрешаемого пакета — в запрещённые."""
        for d, dirs, files in os.walk(top):
            if any(r.search(d) for r in _GRADER_RES) or any(r.search(os.path.join(d, f)) for r in _GRADER_RES for f in files):
                self.forbidden.add(os.path.realpath(d))
                dirs[:] = []
            else:
                dirs[:] = [x for x in dirs if x != "__pycache__"]

    def tree(self, shown: str, kind: str, scan: bool = False) -> None:
        real = os.path.realpath(shown)
        if not os.path.exists(real) or self.blocked(real) or real in self.code:
            return
        if scan and os.path.isdir(real):
            self.scan(real)
            if self.blocked(real):
                return
        if not any(x != real and _within(x, real) for x in self.forbidden | self.code):
            self.rules.append((real, kind))
            return
        self.prefill.append(shown)
        for name in _listdir(real):
            self.tree(os.path.join(shown, name), kind)

    def code_dir(self, shown: str) -> None:
        """Каталог из sys.path вне установки Python (корень проекта, папка скрипта, cwd): открываются
        только подкаталоги-пакеты (с __init__) и файлы модулей, не данные рядом с ними."""
        real = os.path.realpath(shown)
        if not os.path.exists(real) or self.blocked(real):
            return
        if not os.path.isdir(real):  # zip-архив в sys.path
            self.rules.append((real, "r"))
            return
        self.prefill.append(shown)
        suffixes = tuple(importlib.machinery.all_suffixes())
        for name in _listdir(real):
            child = os.path.join(shown, name)
            if os.path.isdir(child):
                if any(os.path.isfile(os.path.join(child, "__init__" + x)) for x in suffixes):
                    self.tree(child, "r", scan=True)
            elif name.endswith(suffixes):
                self.tree(child, "r")


def landlock_rules(private_dir: str, path=None, forbidden=()) -> dict:
    """План песочницы Landlock процесса политики: {"rules": [[путь, вид]], "prefill": [каталоги]}
    (вид — ключ landlock.ACCESS). ``path`` — sys.path процесса политики, ``forbidden`` — каталоги
    эпизода, которые нельзя открыть ни при каких правилах (к ним всегда добавляются episodes/ проекта
    и журнал оценщика AUDITGYM_GRADER_LOG)."""
    path = [os.path.abspath(p) for p in (sys.path if path is None else path) if isinstance(p, str)]
    libs = _library_dirs()
    entries = list(dict.fromkeys(path + [str(ROOT)]))
    code = [e for e in entries if not any(_within(os.path.realpath(e), lib) for lib in libs)]
    deny = [str(ROOT / "episodes"), *map(os.fspath, forbidden)]
    if os.environ.get("AUDITGYM_GRADER_LOG"):
        deny.append(os.path.abspath(os.environ["AUDITGYM_GRADER_LOG"]))
    plan = _LandlockPlan(deny, code)
    exe = [os.path.realpath(sys.executable)] if sys.executable else []
    for t in [*_LL_SYSTEM_RX, sysconfig.get_config_var("LIBDIR") or "/nonexistent", *exe]:
        plan.tree(t, "rx")
    for t in sorted(libs) + [e for e in entries if e not in code]:
        plan.tree(t, "r")
    for c in code:
        plan.code_dir(c)
    for t in _LL_SYSTEM_R:
        plan.tree(t, "r")
    for t in _LL_DEVICES:
        if not plan.blocked(t):
            plan.rules.append((t, "dev"))
    plan.rules += [(os.path.realpath(private_dir), "rw"),
                   ("/proc/self", "r")]  # открывает сам процесс политики: /proc/<его pid>
    return {"rules": [list(r) for r in dict.fromkeys(plan.rules)], "prefill": list(dict.fromkeys(plan.prefill))}


def _enter_sandbox(plan):
    """В процессе политики, до загрузки её кода: заполнить кэш импорта и наложить Landlock.
    Возвращает, что действует: "landlock-abiN" или "none: причина"; None — плана нет (не Linux)."""
    if plan is None:
        return None
    for d in plan.get("prefill", ()):
        try:
            importlib.machinery.PathFinder.find_spec("_auditgym_prefill_", [d])
        except Exception:
            pass
    try:
        version, why = landlock.restrict([tuple(r) for r in plan.get("rules", ())])
    except Exception as e:  # песочница не должна ронять прогон: причина — в результате и журнале
        version, why = 0, "%s: %s" % (type(e).__name__, e)
    return "landlock-abi%d" % version if version else "none: " + why


class _PolicyProcess:
    """Политика в отдельном свежем процессе: те же begin/act, но через канал JSON. В этом процессе нет
    seed и config эпизода, клиента среды с токеном оценщика и кода, читающего итог эпизода; рабочая
    папка и TMPDIR — личная временная папка, удаляемая после эпизода. ``forbidden`` — каталоги эпизода,
    недоступные процессу политики и в песочнице; ``self.sandbox`` — что его ограничивает."""

    def __init__(self, policy, forbidden=()):
        buf = io.BytesIO()
        try:
            _Pickler(buf, pickle.HIGHEST_PROTOCOL).dump(policy)
        except Exception as e:
            raise TypeError("политику нельзя передать в отдельный процесс (%s: %s); доверенный запуск в одном "
                            "процессе — isolated=False" % (type(e).__name__, e)) from e
        self._dir = tempfile.mkdtemp(prefix="auditgym-policy-")
        # переменные оркестратора AUDITGYM_* (журнал оценщика и др.) не передаются; из них у политики
        # только POLICY_PROCESS_ENV, которую ставит run_policy
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("AUDITGYM_")}
        env.update({"PYTHONPATH": str(ROOT), POLICY_PROCESS_ENV: "1", "PYTHONDONTWRITEBYTECODE": "1",
                    "TMPDIR": self._dir, "TMP": self._dir, "TEMP": self._dir})
        # относительные пути sys.path (например, "" = рабочая папка оркестратора) — абсолютными: у процесса
        # политики своя рабочая папка
        path = [os.path.abspath(p) if isinstance(p, str) else p for p in sys.path]
        forbidden = [os.path.abspath(os.fspath(f)) for f in forbidden]
        prefix = _sandbox_prefix(self._dir, forbidden)
        plan = plan_error = None
        if sys.platform.startswith("linux"):
            try:
                plan = landlock_rules(self._dir, path, forbidden)
            except Exception as e:  # без плана — без песочницы, причина в self.sandbox
                plan_error = "%s: %s" % (type(e).__name__, e)
        try:
            self._p = subprocess.Popen(
                prefix + [sys.executable, "-c", "from auditgym.adapters import _serve_policy; _serve_policy()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env, bufsize=1,
                cwd=self._dir, start_new_session=True)
        except BaseException:
            shutil.rmtree(self._dir, ignore_errors=True)
            raise
        try:
            resp = self._call({"path": path, "policy": base64.b64encode(buf.getvalue()).decode("ascii"), "sandbox": plan})
            self.name = str(resp["name"])[:_MAX_NAME]
        except BaseException:
            self.close()
            raise
        if prefix:
            self.sandbox = "sandbox-exec"
        elif plan_error:
            self.sandbox = "none: " + plan_error
        else:
            self.sandbox = str(resp.get("sandbox") or "none: нет песочницы для %s" % sys.platform)[:_MAX_NAME]

    def _call(self, req):
        if self._p.returncode is not None:
            raise RuntimeError("процесс политики остановлен")
        try:
            self._p.stdin.write(json.dumps(req) + "\n")
            self._p.stdin.flush()
            line = self._p.stdout.readline(_MAX_RESPONSE_CHARS + 1)
        except BaseException:  # в том числе прерывание по лимиту времени: процесс политики больше не нужен
            self.close()
            raise
        if not line or (len(line) > _MAX_RESPONSE_CHARS and not line.endswith("\n")):
            self.close()
            raise RuntimeError("процесс политики завершился или прислал слишком длинный ответ")
        resp = json.loads(line)
        if not isinstance(resp, dict) or not resp.get("ok"):
            raise RuntimeError("ошибка политики: %s" % str(resp.get("error") if isinstance(resp, dict) else resp)[:500])
        return resp

    def begin(self, obs):
        self._call({"cmd": "begin", "obs": obs})

    def act(self, obs):
        return self._call({"cmd": "act", "obs": obs}).get("action")

    def close(self):
        if self._p.returncode is None:
            if hasattr(os, "killpg"):  # вместе с процессами, которые политика успела запустить
                try:
                    os.killpg(self._p.pid, signal.SIGKILL)
                except OSError:
                    pass
            self._p.kill()
            self._p.wait()
        for f in (self._p.stdin, self._p.stdout):
            try:
                f.close()
            except OSError:
                pass
        shutil.rmtree(self._dir, ignore_errors=True)


def _serve_policy():
    """Процесс политики: восстановить политику, затем на каждое наблюдение ответить действием."""
    chan_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    chan_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)  # вывод политики идёт в stderr и не смешивается с каналом
    sys.stdout = sys.stderr
    policy = None
    for line in chan_in:
        req = json.loads(line)
        try:
            if policy is None:
                sys.path[:] = req["path"]
                # песочница — до того, как выполнится любой код политики (её модули импортирует pickle)
                sandbox = _enter_sandbox(req.get("sandbox"))
                policy = pickle.loads(base64.b64decode(req["policy"]))
                resp = {"ok": True, "name": str(getattr(policy, "name", type(policy).__name__)), "sandbox": sandbox}
            elif req["cmd"] == "begin":
                policy.begin(req["obs"])
                resp = {"ok": True}
            else:
                resp = {"ok": True, "action": policy.act(req["obs"])}
            out = json.dumps(resp)
        except Exception as e:
            out = json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        chan_out.write(out + "\n")
        chan_out.flush()


def _grader_oracle_requested(policy) -> bool:
    """Явный запрос оркестратора на oracle оценщика: строка "oracle" или экземпляр из make_policy("oracle")."""
    return (type(policy) is str and policy == "oracle") or is_grader_oracle(policy)


_log = logging.getLogger("auditgym.policy")
_warned = set()


def _note_sandbox(sandbox: str) -> str:
    """Политика в отдельном процессе без песочницы — предупреждение в журнал (один раз на причину)."""
    if sandbox.startswith("none") and sandbox not in _warned:
        _warned.add(sandbox)
        _log.warning("процесс политики без песочницы (только отдельный процесс): %s", sandbox)
    return sandbox


def episode_dirs(config) -> list:
    """Каталоги эпизода на диске из config оркестратора (episode_dir, episode_files.root)."""
    if not isinstance(config, dict):
        return []
    files = config.get("episode_files")
    found = [config.get("episode_dir"), files.get("root") if isinstance(files, dict) else None]
    return [os.fspath(p) for p in found if isinstance(p, (str, os.PathLike))]


def run_policy(policy, seed: int = 0, config=None, mode: str = "eval", isolated: bool = True,
               time_limit_s: float = 60.0) -> dict:
    host = env = None
    sandbox = "in-process"
    if _grader_oracle_requested(policy):
        pol = make_policy("oracle")  # свежая oracle среды: ответ — привилегия оценщика, переданный объект не используется
    else:
        pol = as_policy(policy)
        if isolated:
            if os.environ.get(POLICY_PROCESS_ENV):
                raise RuntimeError("run_policy(isolated=True) нельзя вызывать из процесса политики")
            # до запуска среды: процессу политики нечего от неё унаследовать
            host = pol = _PolicyProcess(pol, forbidden=episode_dirs(config))
            sandbox = _note_sandbox(host.sandbox)
    traj = []
    try:
        env = SubprocessEnv(mode=mode, time_limit_s=time_limit_s) if isolated else AuditEnv(mode=mode, time_limit_s=time_limit_s)
        summary = run_episode(env, pol, seed, config, record=traj, time_limit_s=time_limit_s)
        report = env.grader_report()
    finally:
        if host is not None:
            host.close()
        if env is not None:
            env.close()
    return {"summary": summary, "grader": report["result"], "grader_log": report["log"],
            "diagnostics": report.get("diagnostics"), "trajectory": traj, "policy_sandbox": sandbox}


BASELINE_ADAPTERS = {
    "random_explorer": lambda seed=0: make_policy("random", seed),
    "simple_reconciliation": lambda seed=0: make_policy("reconciler"),
    "flag_all_attack": lambda seed=0: make_policy("flag_all"),
    "oracle_grader_only": lambda seed=0: make_policy("oracle"),
}
