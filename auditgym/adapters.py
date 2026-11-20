"""Адаптеры: подключение ЛЮБОЙ политики (в том числе политик организаторов).

Политика может быть
  * функцией ``fn(наблюдение: dict) -> действие: dict``, или
  * объектом с методами ``begin(первое_наблюдение)`` и ``act(наблюдение)``, или
  * именем базовой политики ("random", "reconciler", "flag_all", "oracle", "rule_auditor").

    from auditgym import run_policy
    result = run_policy(my_fn, seed=123, config={"preset": "interacting"}, mode="eval")
    result["grader"]["reward"], result["grader"]["f1"], result["trajectory"]

``run_policy`` запускает среду и оценщика в отдельном процессе (isolated=True), даёт
политике только наблюдения, соблюдает лимит 60 с даже для зависшей политики и возвращает
траекторию и отчёт оценщика (доступный только после эпизода).
"""
from __future__ import annotations

from .env import AuditEnv
from .policies import Oracle, Policy, make_policy
from .runner import run_episode
from .server import SubprocessEnv


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


def run_policy(policy, seed: int = 0, config=None, mode: str = "eval", isolated: bool = True,
               time_limit_s: float = 60.0) -> dict:
    pol = as_policy(policy)
    env = SubprocessEnv(mode=mode, time_limit_s=time_limit_s) if isolated else AuditEnv(mode=mode, time_limit_s=time_limit_s)
    traj = []
    try:
        summary = run_episode(env, pol, seed, config, record=traj, time_limit_s=time_limit_s)
        report = env.grader_report()
    finally:
        if isolated:
            env.close()
    return {"summary": summary, "grader": report["result"], "grader_log": report["log"], "trajectory": traj}


BASELINE_ADAPTERS = {
    "random_explorer": lambda seed=0: make_policy("random", seed),
    "simple_reconciliation": lambda seed=0: make_policy("reconciler"),
    "flag_all_attack": lambda seed=0: make_policy("flag_all"),
    "oracle_grader_only": lambda seed=0: Oracle(),
}
