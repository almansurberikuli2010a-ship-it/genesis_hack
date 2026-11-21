"""Адаптер жюри Genesis FinTech: «Единый способ подключения» определений проверок.

Точки входа (вызывает процесс жюри; рабочая директория — каталог этого файла, корень проекта):

    generate_episode(seed, config, output_dir) -> None
    make_env() -> JudgeEnv            # reset(seed, config) -> (obs, info)
                                      # step(action) -> (obs, reward, terminated, truncated, info)
    score_episode(episode_dir, findings, inspection_steps) -> dict

Каталог эпизода — плоский: manifest.json, ledger.csv, invoices.jsonl, payments.jsonl, fx.csv,
policy.json (открытые, форматы §03) и закрытые truth.json, provenance.json. Других файлов нет:
книги до внесения ошибок восстанавливаются из provenance.json. Загрузка и оценка читают только
эти файлы, поэтому копия с переставленными записями и согласованно заменёнными row_id, doc_id,
invoice_id, payment_id, entity_id (в том числе в truth/provenance) работает так же. Ключ курса
fx_id = FX-<date>-<source_currency>-<target_currency> собирается из столбцов fx.csv по
manifest.fx_id_format и при замене ID не меняется.

reset(seed, config), config = {"mode": "closed" | "train", "episode_dir": str?, "team_config": dict?}:
  * есть episode_dir — загружается ровно этот эпизод (seed и team_config не используются, ID не
    меняются); повторный reset начинает его заново;
  * нет episode_dir — эпизод генерируется по seed и team_config (конфиг генератора команды);
  * mode по умолчанию "closed": награда политике — числовой 0 на каждом шаге, включая последний,
    info = {}; "train": на последнем шаге — терминальная награда и info.grading.
Среда и оценщик работают в ОТДЕЛЬНОМ процессе (auditgym.server через SubprocessEnv): эталон
никогда не попадает в память вызывающего процесса, наружу идут только наблюдения.

step(action), action = {"name": <list_records | get_record | get_document | calculator | submit>,
"args": {...}} (или JSON-строка с таким объектом). Аргументы — §04: list_records(table, cursor, limit),
get_record(table, id), get_document(doc_id), calculator(expression), submit(findings) — findings.json
§05. Таблицы: ledger, invoices, payments, fx; id в get_record — row_id | invoice_id (или doc_id) |
payment_id (или doc_id) | fx_id. Неверная оболочка, неизвестное имя или неверные аргументы —
ограниченная ошибка и израсходованный шаг; неверная структура submit завершает эпизод (награда −1
у оценщика).

Наблюдения: первое — {"task", "manifest", "policy", "remaining_steps": 100}; следующие —
{"result", "error", "remaining_steps"}. result: list_records → {"records": [...], "next_cursor":
null | str} (первый cursor — null, страница не больше 50 записей); get_record / get_document →
сама запись / документ; calculator → десятичная строка ("0.3" для 0.1+0.2); submit →
{"accepted": true}. error: null или {"code": str, "message": str} — публичный код и короткое
сообщение без путей, seed и закрытых данных; при ошибке result = null.

Пограничные решения: submit — одно из 100 действий, но не входит в inspection_steps; все
предыдущие шаги, в том числе ошибочные, входят. 100 действий без submit — truncated=true и
пустой ответ. Лимит времени — 60 с от начала reset; превышение — truncated=true, пустой ответ.

score_episode(episode_dir, findings, inspection_steps) — закрытый вызов оценщика: общий matcher
(auditgym/matcher.py) и награда clip(2·F1 − 1 − 0.002·inspection_steps, −1, 1). Валидный ответ →
{"tp", "fp", "fn", "precision", "recall", "f1", "return"}; неверная структура → "return": -1.0,
остальные поля null, ошибка — в журнале оценщика (GRADER_LOG, логгер "auditgym.judge" и, если
задана переменная окружения AUDITGYM_GRADER_LOG, строка JSON в этом файле). findings=None —
ответа нет (лимит шагов или времени): пустые выводы.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auditgym.adapters import _PolicyProcess  # noqa: E402
from auditgym.export import load_public_dir, write_flat_episode  # noqa: E402
from auditgym.generator import generate  # noqa: E402
from auditgym.matcher import match_context, score  # noqa: E402
from auditgym.runner import _guarded, _PolicyTimeout  # noqa: E402
from auditgym.server import RemoteError, SubprocessEnv  # noqa: E402

ACTION_NAMES = ("list_records", "get_record", "get_document", "calculator", "submit")
TABLES = ("ledger", "invoices", "payments", "fx")
MODES = ("closed", "train")
TIME_LIMIT_S = 60.0
METRIC_KEYS = ("tp", "fp", "fn", "precision", "recall", "f1")

GRADER_LOG: list[dict] = []
_log = logging.getLogger("auditgym.judge")

_ERR_ENVELOPE = {"code": "invalid_action", "message": "действие — объект {\"name\": str, \"args\": object}"}
_ERR_UNKNOWN = {"code": "unknown_action", "message": "name — одно из: " + ", ".join(ACTION_NAMES)}
_ERR_ARGS = {"code": "invalid_arguments", "message": "args должен быть объектом без ключа type"}


# ------------------------------------------------------------------ генерация

def generate_episode(seed: int, config: dict, output_dir: str) -> None:
    """Сгенерировать эпизод генератором команды и записать его плоско в ``output_dir``.
    ``config`` — конфиг генератора (см. auditgym/config.py) или объект с ключом team_config."""
    cfg = config
    if isinstance(config, dict) and set(config) & {"team_config", "mode", "episode_dir"}:
        cfg = config.get("team_config") or {}
    write_flat_episode(generate(seed, cfg), Path(output_dir))


# ------------------------------------------------------------------ среда

def _reset_config(config):
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise ValueError("config должен быть объектом")
    mode = config.get("mode", "closed")
    if mode not in MODES:
        raise ValueError("mode — closed или train")
    if config.get("episode_dir") is not None:
        d = config["episode_dir"]
        if not isinstance(d, (str, os.PathLike)):
            raise ValueError("episode_dir должен быть путём")
        return mode, {"episode_dir": str(Path(d).resolve())}
    team = config.get("team_config") or {}
    if not isinstance(team, dict):
        raise ValueError("team_config должен быть объектом")
    return mode, team


def _to_internal(action):
    """Оболочка жюри → внутреннее действие среды и, если оболочка неверна, ошибка вместо ответа среды
    (внутрь тогда уходит неверное действие: шаг расходуется так же)."""
    if isinstance(action, (str, bytes, bytearray)):
        try:
            action = json.loads(action)
        except (ValueError, RecursionError, MemoryError):
            return None, _ERR_ENVELOPE
    if not isinstance(action, dict) or "name" not in action or set(action) - {"name", "args"}:
        return None, _ERR_ENVELOPE
    name, args = action["name"], action.get("args", {})
    if not isinstance(name, str) or name not in ACTION_NAMES:
        return None, _ERR_UNKNOWN
    if name == "submit":  # неверные аргументы submit — неверная структура ответа (конец, −1)
        return ({"type": "submit", **args} if isinstance(args, dict) and "type" not in args else {"type": "submit"}), None
    if not isinstance(args, dict) or "type" in args:
        return None, _ERR_ARGS
    return {"type": name, **args}, None


def _result(atype, res):
    if res is None:
        return None
    if atype == "list_records":
        return {"records": res["rows"], "next_cursor": res["next_cursor"]}
    if atype == "get_record":
        return res["record"]
    if atype == "get_document":
        return res["document"]
    if atype == "calculator":
        return res["value"]
    if atype == "submit":
        return {"accepted": True}
    return None


class JudgeEnv:
    """reset/step по протоколу жюри. Сама среда и оценщик — в дочернем процессе auditgym.server."""

    def __init__(self, time_limit_s: float = TIME_LIMIT_S):
        self._time_limit_s = time_limit_s
        self._sub = None
        self._imode = None
        self._mode = None

    def reset(self, seed=None, config=None):
        mode, internal = _reset_config(config)
        imode = "eval" if mode == "closed" else "train"
        if self._sub is None or self._imode != imode:
            self.close()
            self._sub = SubprocessEnv(mode=imode, time_limit_s=self._time_limit_s)
            self._imode = imode
        try:
            obs, info = self._sub.reset(seed, internal)
        except RemoteError as e:
            raise ValueError(str(e)) from None
        self._mode = mode
        first = {"task": obs["task"], "manifest": obs["manifest"], "policy": obs["policy"],
                 "remaining_steps": obs["steps_remaining"]}
        return first, {"episode_id": info["episode_id"], "mode": mode, "env_version": info["env_version"]}

    def step(self, action):
        if self._sub is None or self._mode is None:
            raise RuntimeError("сначала вызовите reset()")
        internal, err_override = _to_internal(action)
        obs, reward, terminated, truncated, info = self._sub.step(internal)
        err = obs["error"]
        if err_override is not None and err is not None and err["code"] == "unknown_action":
            err = dict(err_override)
        out = {"result": None if err is not None else _result(obs["action"], obs["result"]), "error": err,
               "remaining_steps": obs["steps_remaining"]}
        if self._mode == "closed":
            return out, 0.0, bool(terminated), bool(truncated), {}
        return out, float(reward), bool(terminated), bool(truncated), info

    def grader_report(self) -> dict:
        """Только для жюри (не для политики): итог эпизода у оценщика, включая issue_id."""
        if self._sub is None:
            raise RuntimeError("эпизода нет")
        return self._sub.grader_report()

    def close(self) -> None:
        if self._sub is not None:
            self._sub.close()
        self._sub = self._imode = self._mode = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def make_env(time_limit_s: float = TIME_LIMIT_S) -> JudgeEnv:
    return JudgeEnv(time_limit_s=time_limit_s)


# ------------------------------------------------------------------ оценка

def _grader_log(entry: dict) -> None:
    GRADER_LOG.append(entry)
    _log.error("invalid submission: %s", json.dumps(entry, ensure_ascii=False))
    path = os.environ.get("AUDITGYM_GRADER_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def score_episode(episode_dir: str, findings: dict, inspection_steps: int) -> dict:
    if not isinstance(inspection_steps, int) or isinstance(inspection_steps, bool) or inspection_steps < 0:
        raise ValueError("inspection_steps — неотрицательное целое")
    d = Path(episode_dir)
    public = load_public_dir(d)
    truth = json.loads((d / "truth.json").read_text(encoding="utf-8"))
    res = score(findings, truth, match_context(public), inspection_steps)
    if not res["valid"]:
        _grader_log({"event": "invalid_submission", "episode_id": public["manifest"].get("episode_id"),
                     "inspection_steps": inspection_steps, "error": res["error"]})
        return dict({k: None for k in METRIC_KEYS}, **{"return": -1.0})
    return dict({k: res[k] for k in METRIC_KEYS}, **{"return": res["reward"]})


# ------------------------------------------------------------------ базовые политики через адаптер

def oracle_findings(episode_dir: str) -> dict:
    """Oracle жюри: верные выводы из закрытого truth.json (первый допустимый набор доказательств)."""
    d = Path(episode_dir)
    truth = json.loads((d / "truth.json").read_text(encoding="utf-8"))
    eid = json.loads((d / "manifest.json").read_text(encoding="utf-8"))["episode_id"]
    return {"episode_id": eid, "findings": [
        {"type": t["type"], "affected_ids": list(t["affected_ids"]), "claimed_amount_minor": t["expected_amount_minor"],
         "currency": t["currency"], "evidence_ids": list(t["evidence_sets"][0]), "explanation": "oracle"}
        for t in truth["issues"]]}


class ProtocolBridge:
    """Политика проекта (begin/act во внутреннем формате auditgym) поверх протокола жюри: наблюдения
    жюри переводятся во внутренние, действия — в оболочку {"name", "args"}."""

    def __init__(self, policy):
        self.policy = policy
        self._last = None

    @staticmethod
    def _first(obs):
        return {"task": obs["task"], "episode_id": obs["manifest"]["episode_id"], "manifest": obs["manifest"],
                "policy": obs["policy"], "actions": {}, "step": 0, "steps_remaining": obs["remaining_steps"]}

    def begin(self, obs: dict) -> None:
        self._last = None
        self.policy.begin(self._first(obs))

    def act(self, obs: dict) -> dict:
        if "task" in obs:  # первое наблюдение (после reset)
            internal = self._first(obs)
        else:
            a, res = self._last or {}, obs["result"]
            kind = a.get("type")
            if res is not None:
                res = {"list_records": lambda: {"table": a.get("table"), "rows": res["records"],
                                                "next_cursor": res["next_cursor"]},
                       "get_record": lambda: {"table": a.get("table"), "record": res},
                       "get_document": lambda: {"document": res},
                       "calculator": lambda: {"value": res}}.get(kind, lambda: res)()
            internal = {"step": 100 - obs["remaining_steps"], "steps_remaining": obs["remaining_steps"],
                        "action": kind, "result": res, "error": obs["error"]}
        action = self.policy.act(internal)
        self._last = action if isinstance(action, dict) else None
        if not isinstance(action, dict):
            return {"name": None}
        return {"name": action.get("type"), "args": {k: v for k, v in action.items() if k != "type"}}


def run_on_episode(policy, episode_dir: str, mode: str = "closed", time_limit_s: float = TIME_LIMIT_S,
                   isolated: bool = True) -> dict:
    """Прогон политики протокола жюри (begin(obs) / act(obs) -> {"name", "args"}) на эпизоде из каталога
    и оценка score_episode — так, как это делает жюри. Среда и оценщик — в отдельном процессе.
    isolated=True (по умолчанию): политика — в своём ограниченном процессе, как в run_policy
    (sandbox-exec на macOS, Landlock на Linux): каталог эпизода, файлы оценщика и код среды для записи
    ей недоступны; исключение или остановка процесса политики — неверное действие (шаг израсходован).
    isolated=False — доверенный запуск в этом процессе."""
    host = _PolicyProcess(policy, forbidden=[episode_dir]) if isolated else None
    pol = host if host is not None else policy
    try:
        with make_env(time_limit_s) as env:
            obs, _ = env.reset(0, {"mode": mode, "episode_dir": str(episode_dir)})
            # как в run_policy: зависшая политика прерывается в момент deadline, после чего политику
            # больше не спрашиваем — следующий шаг среда завершает с truncated=true (time_limit)
            deadline = time.monotonic() + time_limit_s
            timed_out = False
            try:
                _guarded(lambda: pol.begin(obs), deadline)
            except _PolicyTimeout:
                timed_out = True
            except Exception:
                if not isolated:
                    raise
            steps, findings, rewards = 0, None, []
            while True:
                if timed_out:
                    time.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
                    action = {"name": None}
                else:
                    try:
                        action = _guarded(lambda: pol.act(obs), deadline)
                    except _PolicyTimeout:
                        timed_out = True
                        continue
                    except Exception:
                        if not isolated:
                            raise
                        action = {"name": None}
                obs, r, term, trunc, _ = env.step(action)
                steps += 1
                rewards.append(r)
                if term or trunc:
                    break
            sent = isinstance(action, dict) and action.get("name") == "submit" and term
            if sent:
                findings = (action.get("args") or {}).get("findings")
            inspection = steps - 1 if sent else steps
            result = score_episode(episode_dir, findings if sent else None, inspection)
            report = env.grader_report()["result"]
    finally:
        if host is not None:
            host.close()
    return {"score": result, "inspection_steps": inspection, "steps": steps, "terminated": bool(term),
            "truncated": bool(trunc), "policy_rewards": rewards, "env_result": report,
            "policy_sandbox": host.sandbox if host is not None else "in-process"}


class OraclePolicy:
    """Oracle жюри в протоколе адаптера: ``k`` исследовательских шагов, затем обычный submit."""

    def __init__(self, episode_dir: str, inspection_steps: int = 0):
        self.findings = oracle_findings(episode_dir)
        self.k = inspection_steps
        self.n = 0

    def begin(self, obs):
        self.n = 0

    def act(self, obs):
        self.n += 1
        if self.n <= self.k:
            return {"name": "list_records", "args": {"table": "fx", "cursor": None, "limit": 50}}
        return {"name": "submit", "args": {"findings": self.findings}}


def baseline(name: str, episode_dir: str | None = None, seed: int = 0):
    """Четыре базовые политики (раздел 06) в протоколе адаптера: random, reconciler, flag_all, oracle."""
    if name == "oracle":
        return OraclePolicy(episode_dir)
    from auditgym.policies import make_policy
    if name not in ("random", "reconciler", "flag_all", "rule_auditor"):
        raise ValueError("неизвестная базовая политика")
    return ProtocolBridge(make_policy(name, seed))
