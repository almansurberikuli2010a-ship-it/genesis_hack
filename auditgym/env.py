"""JSON-среда для ИИ-аудитора в стиле Gymnasium.

    reset(seed, config) -> (observation, info)
    step(action)        -> (observation, reward, terminated, truncated, info)

Наблюдения и действия — обычные JSON-совместимые словари. В наблюдения попадают только
открытые записи. Состояние оценщика (эталон, результат, журнал) хранится в ``self._grader``
и доступно только через ``grader_report()`` — его вызывает оркестратор, а не политика, и
только после эпизода. Режим ``eval``: политика получает награду 0 и пустой info. Режим
``train``: на завершающем шаге возвращается терминальная награда (и P/R/F1).
Коды ошибок — латиницей (машиночитаемые), сообщения — на русском. submit с неверной структурой
завершает эпизод (награда −1 у оценщика) и возвращает ограниченную ошибку invalid_submission —
только о структуре, без оценки правильности. Любое неверное действие
(в том числе JSON-строка длиннее ``MAX_ACTION_CHARS``, слишком глубоко вложенная или не
разбираемая) даёт ограниченную ошибку и расходует шаг; исключение наружу не выходит.

Как у Gymnasium: ``close()`` (повторный вызов безопасен) и контекстный менеджер ``with AuditEnv() as env``.
"""
from __future__ import annotations

import copy
import json
import time

from . import calculator
from .config import ConfigError
from .generator import generate
from .matcher import match_context, score
from .policy_text import ACTIONS_HELP, TASK_TEXT

ENV_VERSION = "3.2.0"
MAX_STEPS = 100
PAGE_MAX = 50
MAX_ACTION_CHARS = 1_000_000  # длина действия, переданного JSON-строкой
MAX_ERROR_MESSAGE = 119
TABLES = ("ledger", "invoices", "payments", "fx")
ACTION_TYPES = ("list_records", "get_record", "get_document", "calculator", "submit")
_ID_KEY = {"ledger": "row_id", "invoices": "invoice_id", "payments": "payment_id", "fx": "fx_id"}
_ALLOWED_ARGS = {"list_records": {"table", "cursor", "limit"}, "get_record": {"table", "id"},
                 "get_document": {"doc_id"}, "calculator": {"expression"}}


class _Err(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


_NO_ANSWER = object()
_INVALID = {"__invalid__": "неверно сформированное действие submit"}
_SHOWN_KEYS, _SHOWN_KEY_LEN = 3, 16


def _short(key) -> str:
    k = key if isinstance(key, str) else repr(key)
    return k if len(k) <= _SHOWN_KEY_LEN else k[:_SHOWN_KEY_LEN] + "…"


def _parse_action(action):
    """Действие-строку разобрать как JSON. Слишком длинная, слишком глубоко вложенная или неверная
    строка — неверное действие (None), а не исключение."""
    if not isinstance(action, (str, bytes, bytearray)):
        return action
    if len(action) > MAX_ACTION_CHARS:
        return None
    try:
        return json.loads(action)
    except (ValueError, RecursionError, MemoryError):
        return None


class AuditEnv:
    metadata = {"render_modes": [], "env_version": ENV_VERSION}

    def __init__(self, mode: str = "train", max_steps: int = MAX_STEPS, time_limit_s: float = 60.0, clock=time.monotonic):
        if mode not in ("train", "eval"):
            raise ValueError("mode должен быть 'train' или 'eval'")
        self.mode = mode
        self.max_steps = max_steps
        self.time_limit_s = time_limit_s
        self._clock = clock
        self._ep = None
        self._grader = None

    # ------------------------------------------------------------ API
    def reset(self, seed: int | None = None, config=None, *, options=None):
        """``seed=None`` означает seed 0 (эпизоды всегда воспроизводимы). ``options`` —
        синоним ``config`` в стиле Gymnasium. ``config`` может быть JSON-строкой."""
        t_start = self._clock()  # лимит времени эпизода отсчитывается от начала reset
        if config is None and options is not None:
            config = options
        if isinstance(config, (str, bytes)):
            try:
                config = json.loads(config)
            except ValueError:
                raise ConfigError("конфиг должен быть объектом или JSON-строкой с объектом")
        if isinstance(config, dict) and "episode_dir" in config:
            ep = _episode_from_dir(config)  # эпизод задан плоским каталогом файлов; seed не используется
        elif isinstance(config, dict) and "episode_files" in config:
            ep = _episode_from_files(config)  # эпизод задан файлами; seed не используется
        else:
            ep = generate(seed, config)
        pub = ep.public
        self._ep = ep
        self._tables = {t: pub[t] for t in TABLES}
        # get_record(invoices|payments) принимает и doc_id; при совпадении строк основной ID таблицы
        # (invoice_id / payment_id) важнее
        self._index = {t: {r["doc_id"]: r for r in pub[t]} if t in ("invoices", "payments") else {} for t in TABLES}
        for t in TABLES:
            self._index[t].update({r[_ID_KEY[t]]: r for r in pub[t]})
        self._docs = {r["doc_id"]: r for t in ("invoices", "payments") for r in pub[t]}
        self._steps = 0
        self._done = self._terminated = self._truncated = False
        self._t0 = t_start
        self._grader = {"truth": ep.truth, "context": match_context(pub), "result": None, "log": [], "diagnostics": None}
        obs = {
            "task": TASK_TEXT,
            "episode_id": ep.episode_id,
            "manifest": copy.deepcopy(pub["manifest"]),
            "policy": copy.deepcopy(pub["policy"]),
            "actions": copy.deepcopy(ACTIONS_HELP),
            "step": 0,
            "steps_remaining": self.max_steps,
        }
        return obs, {"episode_id": ep.episode_id, "mode": self.mode, "env_version": ENV_VERSION}

    def step(self, action):
        if self._ep is None:
            raise RuntimeError("сначала вызовите reset()")
        if self._done:
            obs = self._obs(None, None, _Err("episode_finished", "эпизод уже завершён; вызовите reset()"))
            return obs, 0.0, self._terminated, self._truncated, {}
        action = _parse_action(action)  # действие можно передать JSON-строкой
        if self._clock() - self._t0 > self.time_limit_s:  # лимит времени проверяется до действия
            return self._truncate("time_limit")
        self._steps += 1
        atype = action.get("type") if isinstance(action, dict) else None
        if not isinstance(atype, str):
            atype = None
        if atype == "submit":
            return self._submit(action)
        try:
            result, err = self._dispatch(action), None
        except _Err as e:
            result, err = None, e
        except Exception:  # непредусмотренный ввод (в том числе RecursionError, MemoryError): ошибка, шаг израсходован
            result, err = None, _Err("invalid_arguments", "действие не удалось обработать")
        if self._steps >= self.max_steps:
            return self._truncate("step_limit", last=(atype, result, err))
        return self._obs(atype, result, err), 0.0, False, False, {}

    def grader_report(self) -> dict:
        """Только для оценщика/оркестратора: полный результат, включая issue_id. Политике не передавать."""
        if self._grader is None:
            raise RuntimeError("эпизода нет")
        g = self._grader
        return {"episode_id": self._ep.episode_id, "finished": self._done, "terminated": self._terminated,
                "truncated": self._truncated, "result": copy.deepcopy(g["result"]), "log": list(g["log"]),
                "n_issues": len(g["truth"]["issues"]), "diagnostics": copy.deepcopy(g["diagnostics"])}

    def close(self) -> None:
        """Освободить эпизод (как ``Env.close`` в Gymnasium); повторный вызов безопасен. После close
        нужен новый reset()."""
        self._ep = None
        self._grader = None
        self._tables = self._index = self._docs = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def oracle_findings(self) -> dict:
        """Oracle только для оценщика: верный ответ с первым набором доказательств каждой ошибки."""
        if self._grader is None:
            raise RuntimeError("эпизода нет")
        t = self._grader["truth"]
        return {"episode_id": t["episode_id"], "findings": [
            {"type": i["type"], "affected_ids": list(i["affected_ids"]), "claimed_amount_minor": i["expected_amount_minor"],
             "currency": i["currency"], "evidence_ids": list(i["evidence_sets"][0]), "explanation": "oracle"}
            for i in t["issues"]]}

    # ------------------------------------------------------------ внутреннее
    def _obs(self, atype, result, err):
        return {
            "step": self._steps,
            "steps_remaining": max(0, self.max_steps - self._steps),
            "action": atype if atype in ACTION_TYPES else None,
            "result": result,
            "error": None if err is None else {"code": err.code, "message": err.message[:MAX_ERROR_MESSAGE]},
        }

    def _finish(self, payload, reason):
        insp = self._steps - (1 if reason == "submit" else 0)
        try:
            res = score(None if payload is _NO_ANSWER else payload, self._grader["truth"], self._grader["context"], insp)
        except Exception:  # непредусмотренная структура ответа (например, RecursionError) — неверный ответ
            res = score(_INVALID, self._grader["truth"], self._grader["context"], insp)
        res["end_reason"] = reason
        self._grader["result"] = res
        # время — диагностика прогона, а не часть результата: result при повторе тех же seed, config и
        # действий совпадает побайтно
        self._grader["diagnostics"] = {"elapsed_s": round(self._clock() - self._t0, 4)}
        if not res["valid"]:
            self._grader["log"].append({"step": self._steps, "event": "invalid_submission", "error": res["error"]})
        self._grader["log"].append({"step": self._steps, "event": "graded", "reason": reason, "reward": res["reward"]})
        self._done = True
        return res

    def _public_info(self, res):
        if self.mode != "train":
            return {}
        return {"grading": {k: res[k] for k in ("valid", "reward", "precision", "recall", "f1", "inspection_steps")}}

    def _submit(self, action):
        payload = action.get("findings", _INVALID) if set(action) == {"type", "findings"} else _INVALID
        if payload is None:
            payload = _INVALID
        res = self._finish(payload, "submit")
        self._terminated = True
        reward = res["reward"] if self.mode == "train" else 0.0
        # ответ о структуре не зависит от эталона: правильность выводов политике не сообщается
        if res["valid"]:
            obs = self._obs("submit", {"submitted": True}, None)
        else:
            obs = self._obs("submit", None, _Err("invalid_submission", "неверная структура findings: %s" % res["error"]))
        return obs, reward, True, False, self._public_info(res)

    def _truncate(self, reason, last=None):
        res = self._finish(_NO_ANSWER, reason)
        self._truncated = True
        reward = res["reward"] if self.mode == "train" else 0.0
        obs = self._obs(*last) if last is not None else \
            self._obs(None, None, _Err("time_limit", "исчерпан лимит времени эпизода"))
        obs["steps_remaining"] = 0
        return obs, reward, False, True, self._public_info(res)

    def _dispatch(self, a):
        if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
            raise _Err("unknown_action", "action.type должен быть одним из: %s" % ", ".join(ACTION_TYPES))
        t = a["type"]
        extra = set(a) - _ALLOWED_ARGS[t] - {"type"}
        if extra:  # имена ключей приходят от агента: показываются не больше трёх, каждое не длиннее 16 символов
            shown = sorted(_short(k) for k in extra)[:_SHOWN_KEYS]
            raise _Err("invalid_arguments", "лишние аргументы (%d): %s%s" % (
                len(extra), ", ".join(shown), ", …" if len(extra) > _SHOWN_KEYS else ""))
        if t == "list_records":
            return self._list(a.get("table"), a.get("cursor"), a.get("limit", PAGE_MAX))
        if t == "get_record":
            table, rid = a.get("table"), a.get("id")
            if table not in TABLES:
                raise _Err("invalid_arguments", "table должен быть одним из: %s" % ", ".join(TABLES))
            if not isinstance(rid, str) or rid not in self._index[table]:
                raise _Err("not_found", "в таблице %s нет записи с таким id" % table)
            return {"table": table, "record": copy.deepcopy(self._index[table][rid])}
        if t == "get_document":
            did = a.get("doc_id")
            if not isinstance(did, str) or did not in self._docs:
                raise _Err("not_found", "нет документа с таким doc_id")
            return {"document": copy.deepcopy(self._docs[did])}
        try:
            return {"value": calculator.evaluate(a.get("expression"))}
        except calculator.CalcError as e:
            raise _Err("calculator_error", str(e))

    def _list(self, table, cursor, limit):
        if table not in TABLES:
            raise _Err("invalid_arguments", "table должен быть одним из: %s" % ", ".join(TABLES))
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise _Err("invalid_arguments", "limit — целое число от 1 (больше %d обрезается)" % PAGE_MAX)
        limit = min(limit, PAGE_MAX)
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.startswith(table + ":"):
                raise _Err("invalid_cursor", "курсор не относится к этой таблице")
            try:
                offset = int(cursor.split(":", 1)[1])
            except ValueError:
                raise _Err("invalid_cursor", "неверный формат курсора")
            if offset < 0 or offset > len(self._tables[table]):
                raise _Err("invalid_cursor", "курсор вне диапазона")
        rows = self._tables[table][offset:offset + limit]
        nxt = offset + len(rows)
        return {"table": table, "rows": copy.deepcopy(rows), "total": len(self._tables[table]),
                "next_cursor": "%s:%d" % (table, nxt) if nxt < len(self._tables[table]) else None}


def _episode_from_dir(config):
    """Эпизод из плоского каталога (judge_adapter.generate_episode): manifest.json, ledger.csv,
    invoices.jsonl, payments.jsonl, fx.csv, policy.json, truth.json, provenance.json. Записи и ID
    берутся из файлов как есть (их могли переставить и переименовать снаружи)."""
    from pathlib import Path

    from .export import load_flat_episode
    from .generator import Episode
    d = config["episode_dir"]
    if set(config) != {"episode_dir"} or not isinstance(d, str):
        raise ConfigError("episode_dir должен быть строкой и единственным ключом")
    try:
        public, clean, truth, prov = load_flat_episode(Path(d))
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigError("не удалось прочитать файлы эпизода")
    return Episode(public, truth, prov, clean, {"source": "episode_dir"})


def _episode_from_files(config):
    """Эпизод из экспортированных (в том числе внешне преобразованных) файлов."""
    from pathlib import Path

    from .export import load_episode_files
    from .generator import Episode
    spec = config["episode_files"]
    if set(config) != {"episode_files"} or not isinstance(spec, dict) or set(spec) != {"root", "episode_id"}:
        raise ConfigError("episode_files должен быть {'root': str, 'episode_id': str} и единственным ключом")
    try:
        public, clean, truth, prov = load_episode_files(Path(spec["root"]), spec["episode_id"])
    except (OSError, ValueError, KeyError):
        raise ConfigError("не удалось прочитать файлы эпизода")
    return Episode(public, truth, prov, clean, {"source": "files"})
