"""Цикл эпизода, наборы прогонов, запись траекторий, сторожевой таймер политики."""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from .env import AuditEnv
from .policies import is_grader_oracle, make_policy
from .server import SubprocessEnv

ROOT = Path(__file__).resolve().parent.parent


def load_split(split: str) -> list[dict]:
    return json.loads((ROOT / "configs" / ("%s.json" % split)).read_text(encoding="utf-8"))["episodes"]


class _PolicyTimeout(Exception):
    pass


def _guarded(fn, deadline):
    """Выполнить fn(), но прервать его в момент ``deadline`` (главный поток, POSIX): зависшая
    политика не может задержать эпизод дольше лимита времени."""
    import signal
    import threading
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _PolicyTimeout()
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        return fn()

    def on_alarm(signum, frame):
        raise _PolicyTimeout()
    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def run_episode(env, policy, seed, config, split: str = "", record: list | None = None, time_limit_s: float = 60.0) -> dict:
    """Цикл оркестратора. Политика получает только наблюдения."""
    t0 = time.perf_counter()
    obs, info = env.reset(seed, config)
    deadline = time.monotonic() + time_limit_s
    if is_grader_oracle(policy):  # привилегия только oracle, созданной самой средой (make_policy("oracle"))
        policy.set_findings(env.oracle_findings())
    try:
        _guarded(lambda: policy.begin(obs), deadline)
    except Exception:  # зависание или падение политики в begin не роняет прогон: дальше её act
        pass
    ep_id = info["episode_id"]
    traj = [{"episode_id": ep_id, "policy": policy.name, "split": split, "step": 0, "action": None,
             "observation": obs, "reward": 0.0, "terminated": False, "truncated": False}]
    total = 0.0
    while True:
        try:
            action = _guarded(lambda: policy.act(obs), deadline)
        except _PolicyTimeout:
            # лимит времени исчерпан внутри политики: дождаться часов среды — любой шаг тогда
            # завершится с truncated=true и пустым ответом
            time.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
            action = {"type": "__timeout__"}
        except Exception:  # падение политики не роняет прогон: действие считается неверным
            action = {"type": "__policy_error__"}
        obs, reward, term, trunc, info = env.step(action)
        total += reward
        traj.append({"episode_id": ep_id, "policy": policy.name, "split": split, "step": obs["step"], "action": action,
                     "observation": obs, "reward": reward, "terminated": term, "truncated": trunc})
        if term or trunc:
            break
    rep = env.grader_report()
    res = rep["result"]
    if record is not None:
        record.extend(traj)
    return {
        "episode_id": ep_id, "policy": policy.name, "split": split,
        "reward": res["reward"], "experimental_reward": res["experimental_reward"], "policy_return": total,
        "precision": res["precision"], "recall": res["recall"], "f1": res["f1"],
        "tp": res["tp"], "fp": res["fp"], "fn": res["fn"], "valid": res["valid"],
        "steps": len(traj) - 1, "inspection_steps": res["inspection_steps"], "end_reason": res["end_reason"],
        "time_s": round(time.perf_counter() - t0, 4), "n_issues": rep["n_issues"],
        "missed_issues": res["missed_issues"], "unmatched_findings": res["unmatched_findings"],
        "truncated": rep["truncated"],
    }


def run_suite(entries, policies, mode="eval", isolated=True, split="", record=None, seed=0):
    results = []
    env = SubprocessEnv(mode=mode) if isolated else AuditEnv(mode=mode)
    try:
        for e in entries:
            for name in policies:
                results.append(run_episode(env, make_policy(name, seed), e["seed"], e["config"], split, record))
    finally:
        if isolated:
            env.close()
    return results


def summarize(results) -> dict:
    out = {}
    for name in sorted({r["policy"] for r in results}):
        rs = [r for r in results if r["policy"] == name]
        out[name] = {
            "episodes": len(rs),
            "precision": round(statistics.mean(r["precision"] for r in rs), 3),
            "recall": round(statistics.mean(r["recall"] for r in rs), 3),
            "f1": round(statistics.mean(r["f1"] for r in rs), 3),
            "return": round(statistics.mean(r["reward"] for r in rs), 3),
            "experimental_return": round(statistics.mean(r["experimental_reward"] for r in rs), 3),
            "steps": round(statistics.mean(r["steps"] for r in rs), 1),
            "time_s": round(statistics.mean(r["time_s"] for r in rs), 3),
            "tp": sum(r["tp"] for r in rs), "fp": sum(r["fp"] for r in rs), "fn": sum(r["fn"] for r in rs),
            "invalid": sum(1 for r in rs if not r["valid"]),
        }
    return out


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
