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
оркестратора. На macOS процессу политики запрещена запись в код среды и туда, откуда Python
загружает код. Лимит 60 с соблюдается даже для зависшей политики; возвращаются траектория и отчёт
оценщика (доступный только после эпизода).
"""
from __future__ import annotations

import abc
import base64
import builtins
import importlib
import io
import json
import marshal
import os
import pickle
import subprocess
import sys
import types

from .env import AuditEnv
from .policies import Oracle, Policy, make_policy
from .runner import ROOT, run_episode
from .server import SubprocessEnv

POLICY_PROCESS_ENV = "AUDITGYM_POLICY_PROCESS"
_HEAPTYPE = 1 << 9  # Py_TPFLAGS_HEAPTYPE: класс описан на Python, а не встроен в интерпретатор


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


def _write_sandbox() -> list:
    """Начало команды процесса политики. На macOS (sandbox-exec) ему запрещена запись в код среды и
    оценщика и туда, откуда Python загружает код (sys.path, установка Python, пользовательский
    site-packages): изменённый код не попадёт в следующие процессы среды. На других системах запись
    ограничивают права на файлы (в Docker-образе код принадлежит root)."""
    exe = "/usr/bin/sandbox-exec"
    if sys.platform != "darwin" or not os.path.exists(exe):
        return []
    import site
    paths = {str(ROOT), sys.prefix, sys.base_prefix, sys.exec_prefix, site.getusersitepackages(),
             *(p for p in sys.path if isinstance(p, str))}
    rules = "".join(' (subpath "%s")' % os.path.realpath(p).replace("\\", "\\\\").replace('"', '\\"')
                    for p in sorted(paths))
    return [exe, "-p", "(version 1) (allow default) (deny file-write*%s)" % rules]


class _PolicyProcess:
    """Политика в отдельном свежем процессе: те же begin/act, но через канал JSON. В этом процессе нет
    seed и config эпизода, клиента среды с токеном оценщика и кода, читающего итог эпизода."""

    def __init__(self, policy):
        buf = io.BytesIO()
        try:
            _Pickler(buf, pickle.HIGHEST_PROTOCOL).dump(policy)
        except Exception as e:
            raise TypeError("политику нельзя передать в отдельный процесс (%s: %s); доверенный запуск в одном "
                            "процессе — isolated=False" % (type(e).__name__, e)) from e
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update({"PYTHONPATH": str(ROOT), POLICY_PROCESS_ENV: "1"})
        self._p = subprocess.Popen(
            _write_sandbox() + [sys.executable, "-c", "from auditgym.adapters import _serve_policy; _serve_policy()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env, bufsize=1)
        try:
            self.name = self._call({"path": sys.path, "policy": base64.b64encode(buf.getvalue()).decode("ascii")})["name"]
        except BaseException:
            self.close()
            raise

    def _call(self, req):
        if self._p.returncode is not None:
            raise RuntimeError("процесс политики остановлен")
        try:
            self._p.stdin.write(json.dumps(req) + "\n")
            self._p.stdin.flush()
            line = self._p.stdout.readline()
        except BaseException:  # в том числе прерывание по лимиту времени: процесс политики больше не нужен
            self.close()
            raise
        if not line:
            self.close()
            raise RuntimeError("процесс политики завершился")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise RuntimeError("ошибка политики: %s" % resp.get("error"))
        return resp

    def begin(self, obs):
        self._call({"cmd": "begin", "obs": obs})

    def act(self, obs):
        return self._call({"cmd": "act", "obs": obs})["action"]

    def close(self):
        if self._p.returncode is None:
            self._p.kill()
            self._p.wait()
        for f in (self._p.stdin, self._p.stdout):
            try:
                f.close()
            except OSError:
                pass


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
                policy = pickle.loads(base64.b64decode(req["policy"]))
                resp = {"ok": True, "name": str(policy.name)}
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


def run_policy(policy, seed: int = 0, config=None, mode: str = "eval", isolated: bool = True,
               time_limit_s: float = 60.0) -> dict:
    pol = as_policy(policy)
    host = env = None
    if isolated and type(pol) is not Oracle:  # базовая oracle — часть оценщика, ей ответ положен
        if os.environ.get(POLICY_PROCESS_ENV):
            raise RuntimeError("run_policy(isolated=True) нельзя вызывать из процесса политики")
        host = pol = _PolicyProcess(pol)  # до запуска среды: процессу политики нечего от неё унаследовать
    traj = []
    try:
        env = SubprocessEnv(mode=mode, time_limit_s=time_limit_s) if isolated else AuditEnv(mode=mode, time_limit_s=time_limit_s)
        summary = run_episode(env, pol, seed, config, record=traj, time_limit_s=time_limit_s)
        report = env.grader_report()
    finally:
        if host is not None:
            host.close()
        if isolated and env is not None:
            env.close()
    return {"summary": summary, "grader": report["result"], "grader_log": report["log"], "trajectory": traj}


BASELINE_ADAPTERS = {
    "random_explorer": lambda seed=0: make_policy("random", seed),
    "simple_reconciliation": lambda seed=0: make_policy("reconciler"),
    "flag_all_attack": lambda seed=0: make_policy("flag_all"),
    "oracle_grader_only": lambda seed=0: Oracle(),
}
