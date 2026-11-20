"""Конфигурация эпизода: значения по умолчанию, именованные пресеты и строгая проверка.

Конфиг можно передать словарём, JSON-строкой или ``None``. Ключ ``"preset"`` подставляет
именованный набор значений, остальные ключи его переопределяют. Результат нормализации
никогда не содержит ``preset``: развёрнутый пресет и эквивалентный явный конфиг дают
побайтно одинаковый эпизод.
"""
from __future__ import annotations

import copy
import json

GENERATOR_VERSION = "3.0.0"
SCHEMA_VERSION = "3.0"
CONFIG_VERSION = "3.0"

ERROR_TYPES = ("duplicate_invoice", "payment_allocation", "fx_rate", "period_cutoff")
INTERACTIONS = ("partial_fx_boundary", "masked_duplicate")
DISTRACTORS = (
    "split_payment",          # счёт оплачен двумя платежами
    "multi_invoice_payment",  # один платёж на несколько счетов
    "partial_open",           # частичная оплата, остаток открыт
    "refund",                 # переплата и возврат (отрицательный платёж)
    "recurring_amount",       # еженедельные счета на одинаковую сумму
    "tz_boundary",            # напечатанная дата в другом месяце, момент — внутри периода
    "unremitted_payment",     # платёж без расписки (invoice_refs = null)
    "next_period_doc",        # документ следующего периода, правильно НЕ проведён
    "same_day_repeat",        # два одинаковых заказа в один день с разными PO
    "prior_period_doc",       # счёт прошлого периода, правильно НЕ проведён
)
MAX_ISSUES = 12
MAX_DOCUMENTS = 200
MAX_ATTEMPTS = 12

DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,
    "n_customers": [8, 16],
    "n_invoices": [60, 95],
    "background_entries": [95, 130],
    "foreign_share": 0.45,
    "errors": {t: 0 for t in ERROR_TYPES},
    "interactions": {t: 0 for t in INTERACTIONS},
    "distractors": {
        "split_payment": 2,
        "multi_invoice_payment": 2,
        "partial_open": 2,
        "refund": 1,
        "recurring_amount": 1,
        "tz_boundary": 2,
        "unremitted_payment": 1,
        "next_period_doc": 2,
        "same_day_repeat": 1,
        "prior_period_doc": 1,
    },
    "id_salt": "default",
    "order_seed": 0,
}

_NO_ERRORS = {t: 0 for t in ERROR_TYPES}
_NO_INTER = {t: 0 for t in INTERACTIONS}

PRESETS = {
    # без ошибок, отвлекающие случаи по умолчанию
    "clean": {"errors": dict(_NO_ERRORS), "interactions": dict(_NO_INTER)},
    # без ошибок, все виды отвлекающих случаев с высокой интенсивностью
    "clean_hard": {"errors": dict(_NO_ERRORS), "interactions": dict(_NO_INTER), "foreign_share": 0.6,
                   "distractors": {"split_payment": 3, "multi_invoice_payment": 3, "partial_open": 3, "refund": 2,
                                   "recurring_amount": 2, "tz_boundary": 4, "unremitted_payment": 2,
                                   "next_period_doc": 3, "same_day_repeat": 2, "prior_period_doc": 2}},
    # оба сценария взаимодействующих ошибок (4 ошибки) + одна независимая
    "interacting": {"errors": dict(_NO_ERRORS, fx_rate=1), "interactions": {"partial_fx_boundary": 1, "masked_duplicate": 1}},
    "interacting_partial_fx_boundary": {"errors": dict(_NO_ERRORS), "interactions": {"partial_fx_boundary": 1, "masked_duplicate": 0}},
    "interacting_masked_duplicate": {"errors": dict(_NO_ERRORS), "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 1}},
    # по одной ошибке каждого независимого типа
    "four_types": {"errors": {t: 1 for t in ERROR_TYPES}, "interactions": dict(_NO_INTER)},
    # типичный смешанный эпизод
    "mixed": {"errors": {"duplicate_invoice": 1, "payment_allocation": 2, "fx_rate": 2, "period_cutoff": 1},
              "interactions": {"partial_fx_boundary": 0, "masked_duplicate": 1}},
    # максимальная нагрузка: 12 ошибок
    "heavy": {"errors": {t: 2 for t in ERROR_TYPES}, "interactions": {"partial_fx_boundary": 1, "masked_duplicate": 1}},
}
for _t in ERROR_TYPES:
    PRESETS["only_" + _t] = {"errors": dict(_NO_ERRORS, **{_t: 2}), "interactions": dict(_NO_INTER)}


class ConfigError(ValueError):
    """Неверная конфигурация (неизвестный ключ, значение вне границ и т. п.)."""


def normalize_config(config: dict | str | bytes | None) -> dict:
    if isinstance(config, (str, bytes)):
        try:
            config = json.loads(config)
        except ValueError:
            raise ConfigError("конфиг должен быть объектом или JSON-строкой с объектом")
    if config is not None and not isinstance(config, dict):
        raise ConfigError("конфиг должен быть объектом")
    config = copy.deepcopy(config or {})
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if "preset" in config:
        name = config.pop("preset")
        if not isinstance(name, str) or name not in PRESETS:
            raise ConfigError("неизвестный пресет; доступны: %s" % ", ".join(sorted(PRESETS)))
        cfg = _overlay(cfg, copy.deepcopy(PRESETS[name]))
    cfg = _overlay(cfg, config)
    return _validate(cfg)


def _overlay(cfg: dict, config: dict) -> dict:
    for k, v in config.items():
        if k not in cfg:
            raise ConfigError("неизвестный ключ конфига: %s" % k)
        if isinstance(cfg[k], dict):
            if not isinstance(v, dict):
                raise ConfigError("%s должен быть объектом" % k)
            for kk, vv in v.items():
                if kk not in cfg[k]:
                    raise ConfigError("неизвестный ключ %s.%s" % (k, kk))
                if not isinstance(vv, int) or isinstance(vv, bool) or not 0 <= vv <= MAX_ISSUES:
                    raise ConfigError("%s.%s должен быть целым числом от 0 до %d" % (k, kk, MAX_ISSUES))
                cfg[k][kk] = vv
        else:
            cfg[k] = v
    return cfg


def _validate(cfg: dict) -> dict:
    if cfg["config_version"] != CONFIG_VERSION:
        raise ConfigError("неподдерживаемая config_version %r (нужна %r)" % (cfg["config_version"], CONFIG_VERSION))
    for key, lo, hi in (("n_customers", 3, 40), ("n_invoices", 20, 120), ("background_entries", 0, 200)):
        r = cfg[key]
        if (not isinstance(r, list) or len(r) != 2 or not all(isinstance(x, int) and not isinstance(x, bool) for x in r)
                or not lo <= r[0] <= r[1] <= hi):
            raise ConfigError("%s должен быть [от, до] в пределах [%d, %d]" % (key, lo, hi))
    fs = cfg["foreign_share"]
    if isinstance(fs, bool) or not isinstance(fs, (int, float)) or not 0 <= fs <= 1:
        raise ConfigError("foreign_share должен быть числом от 0 до 1")
    if not isinstance(cfg["id_salt"], str) or not 0 < len(cfg["id_salt"]) <= 64:
        raise ConfigError("id_salt должен быть непустой строкой до 64 символов")
    if not isinstance(cfg["order_seed"], int) or isinstance(cfg["order_seed"], bool):
        raise ConfigError("order_seed должен быть целым числом")
    n_issues = sum(cfg["errors"].values()) + 2 * sum(cfg["interactions"].values())
    if n_issues > MAX_ISSUES:
        raise ConfigError("не больше %d ошибок на эпизод (сценарий взаимодействия считается за 2)" % MAX_ISSUES)
    return cfg


def story_part(cfg: dict) -> dict:
    """Часть конфига, определяющая экономику истории (без безвредных преобразований)."""
    return {k: v for k, v in cfg.items() if k not in ("id_salt", "order_seed")}
