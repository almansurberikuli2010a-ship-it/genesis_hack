"""AuditGym — воспроизводимая RL-среда для дообучения ИИ-аудитора финансовой отчётности.

Быстрый API
-----------
    from auditgym import make_env, run_policy, generate, score
    env = make_env(mode="train")            # в процессе; make_env(isolated=True) — отдельный процесс
    obs, info = env.reset(seed=1, config={"preset": "mixed"})
    obs, reward, terminated, truncated, info = env.step({"type": "list_records", "table": "ledger"})
"""
from .config import CONFIG_VERSION, GENERATOR_VERSION, PRESETS, SCHEMA_VERSION, ConfigError
from .env import ENV_VERSION, MAX_STEPS, AuditEnv
from .generator import generate
from .matcher import metrics, score, terminal_reward

__version__ = ENV_VERSION
__all__ = ["AuditEnv", "SubprocessEnv", "make_env", "run_policy", "generate", "score", "metrics", "terminal_reward",
           "PRESETS", "ConfigError", "ENV_VERSION", "GENERATOR_VERSION", "SCHEMA_VERSION", "CONFIG_VERSION", "MAX_STEPS"]


def make_env(mode: str = "train", isolated: bool = False, time_limit_s: float = 60.0):
    """isolated=True — среда и оценщик в отдельном процессе (официальная проверка)."""
    if isolated:
        from .server import SubprocessEnv
        return SubprocessEnv(mode=mode, time_limit_s=time_limit_s)
    return AuditEnv(mode=mode, time_limit_s=time_limit_s)


def __getattr__(name):  # ленивый импорт: `import auditgym` остаётся лёгким и без циклов
    if name == "SubprocessEnv":
        from .server import SubprocessEnv
        return SubprocessEnv
    if name == "run_policy":
        from .adapters import run_policy
        return run_policy
    raise AttributeError(name)
