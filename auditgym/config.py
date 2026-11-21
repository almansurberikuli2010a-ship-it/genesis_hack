"""Конфигурация эпизода: значения по умолчанию, именованные пресеты и строгая проверка.

Конфиг можно передать словарём, JSON-строкой или ``None``. Ключ ``"preset"`` подставляет
именованный набор значений, остальные ключи его переопределяют. Результат нормализации
никогда не содержит ``preset``: развёрнутый пресет и эквивалентный явный конфиг дают
побайтно одинаковый эпизод.

Что гарантируется для любого конфига, который принимает ``normalize_config``:
  * ``errors`` и ``interactions`` выполняются ТОЧНО (каждая ошибка размещается; не хватает
    подходящей цели — генератор добавляет в историю корректный документ-цель);
  * ``distractors`` выполняются ТОЧНО (не хватает свободного счёта — случай строится на своём
    счёте);
  * ``n_customers`` и ``background_entries`` выполняются точно;
  * ``n_invoices`` — число ОБЫЧНЫХ счетов до отвлекающих случаев и ошибок; оно уменьшается,
    чтобы эпизод остался около предлагаемого ТЗ размера TARGET_DOCUMENTS = 200 документов
    (отвлекающие случаи и ошибки при этом выполняются точно и могут увести эпизод выше 200),
    и в любом случае — чтобы документов было не больше MAX_DOCUMENTS (фактическое число — в
    provenance.actual.regular_invoices);
  * ``foreign_share`` — вероятность для каждого клиента вести счета в иностранной валюте; если
    заказаны fx_rate или partial_fx_boundary, а иностранного клиента не выпало, первый клиент
    становится иностранным (фактическая доля — в provenance.actual);
  * документов не больше MAX_DOCUMENTS = 400. Конфиг, которому даже без обычных счетов может
    понадобиться больше (см. ``document_bound``), отвергается ConfigError; при нынешних ценах
    случаев (DISTRACTOR_DOCS, ISSUE_DOCS) граница самого тяжёлого допустимого конфига — ровно 400,
    поэтому принимается любой конфиг в опубликованных границах;
  * ``variants`` задаёт вариант каждой НЕЗАВИСИМОЙ ошибки типа (``errors``; сценарии
    взаимодействия свои варианты не меняют), например
    ``{"errors": {"payment_allocation": 1}, "variants": {"payment_allocation": "partial"}}``:
      payment_allocation: missing (расписанный платёж целиком остался на 2900) | partial (часть)
      fx_rate:            wrong_date (курс другой даты) | rounded_rate (вручную округлённый курс)
                          | inverted_rate (1 / курс; только для пар валют около паритета)
      period_cutoff:      prior_period (документ прошлого периода проведён поздно)
                          | next_period (документ следующего периода проведён задним числом)
    "random" (по умолчанию) — вариант выбирается случайно, как без ключа ``variants``. Конфиг без
    ``variants`` или со всеми "random" даёт побайтно тот же эпизод, что и до появления ключа.
    Фактический вариант каждой ошибки — в provenance.mutations[].variant.
"""
from __future__ import annotations

import copy
import json

GENERATOR_VERSION = "3.3.0"
SCHEMA_VERSION = "3.2"
CONFIG_VERSION = "3.3"

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
    "boundary_fx_partial",    # частичный валютный платёж в последний день периода, у смены даты по UTC;
                              # проведён верно (двойник сценария partial_fx_boundary без ошибок)
)
# Варианты независимых ошибок (ключ конфига "variants"); "random" — случайный выбор генератора.
VARIANTS = {
    "payment_allocation": ("missing", "partial"),
    "fx_rate": ("wrong_date", "rounded_rate", "inverted_rate"),
    "period_cutoff": ("prior_period", "next_period"),
}
RANDOM_VARIANT = "random"
MAX_ISSUES = 12
MAX_DOCUMENTS = 400              # жёсткий предел документов эпизода
TARGET_DOCUMENTS = 200           # предлагаемый ТЗ размер: под него подбирается число обычных счетов
MAX_ATTEMPTS = 12

# Наибольшее число документов, которое может добавить один случай сверх обычных счетов (обычный
# счёт — не больше двух документов: сам счёт и его оплата). Случай, взявший обычный счёт, тратит
# его «два документа» и не больше своей цены сверху. Сумма даёт строгую верхнюю границу размера.
DISTRACTOR_DOCS = {
    "split_payment": 3,          # счёт + 2 платежа
    "multi_invoice_payment": 3,  # 2 счёта + 1 платёж
    "partial_open": 2,           # счёт + платёж
    "refund": 3,                 # счёт + переплата + возврат
    "recurring_amount": 6,       # 3 счёта + их оплаты
    "tz_boundary": 2,            # счёт (+ оплата) или счёт + платёж у границы
    "unremitted_payment": 2,     # счёт + платёж без расписки
    "next_period_doc": 2,        # счёт следующего периода или счёт + оплата после конца
    "same_day_repeat": 4,        # 2 счёта + их оплаты
    "prior_period_doc": 1,       # счёт прошлого периода
    "boundary_fx_partial": 2,    # свой счёт + частичный платёж
}
ISSUE_DOCS = {
    "duplicate_invoice": 2,      # копия (+ счёт-цель, если подходящего нет)
    "payment_allocation": 2,     # счёт-цель + платёж-цель, если подходящих нет
    "fx_rate": 3,                # запас валютных счетов с оплатой (2) + счёт-цель, если подходящего нет
    "period_cutoff": 1,          # документ соседнего периода
    "partial_fx_boundary": 2,    # валютный счёт + частичная оплата на границе
    "masked_duplicate": 3,       # копия + счёт и платёж-цель, если подходящих нет
}
FX_RESERVE = 2                   # запас валютных счетов сверх числа fx_rate (см. story.build_story)

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
        "boundary_fx_partial": 1,
    },
    "variants": {t: RANDOM_VARIANT for t in VARIANTS},
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
                                   "next_period_doc": 3, "same_day_repeat": 2, "prior_period_doc": 2,
                                   "boundary_fx_partial": 2}},
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
        if k == "variants":
            if not isinstance(v, dict):
                raise ConfigError("variants должен быть объектом")
            for kk, vv in v.items():
                if kk not in VARIANTS:
                    raise ConfigError("неизвестный ключ variants.%s; доступны: %s" % (kk, ", ".join(VARIANTS)))
                if vv != RANDOM_VARIANT and vv not in VARIANTS[kk]:
                    raise ConfigError("variants.%s: одно из %s" % (kk, ", ".join((RANDOM_VARIANT,) + VARIANTS[kk])))
                cfg[k][kk] = vv
        elif isinstance(cfg[k], dict):
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
    need = document_bound(cfg)
    if need > MAX_DOCUMENTS:
        raise ConfigError("отвлекающие случаи и ошибки могут потребовать до %d документов даже без обычных счетов, "
                          "а эпизод ограничен %d документами; уменьшите distractors (цена случая в документах: %s)"
                          % (need, MAX_DOCUMENTS, ", ".join("%s=%d" % kv for kv in DISTRACTOR_DOCS.items())))
    return cfg


def document_bound(cfg: dict) -> int:
    """Строгая верхняя граница числа документов эпизода БЕЗ обычных счетов.
    Документов всего не больше ``document_bound(cfg) + 2 × обычных счетов``."""
    e, it = cfg["errors"], cfg["interactions"]
    total = sum(DISTRACTOR_DOCS[k] * v for k, v in cfg["distractors"].items())
    total += sum(ISSUE_DOCS[k] * v for k, v in e.items()) + sum(ISSUE_DOCS[k] * v for k, v in it.items())
    if e["fx_rate"]:
        total += 2 * FX_RESERVE
    return total


def safe_regular_invoices(cfg: dict) -> int:
    """Сколько обычных счетов гарантированно помещается в MAX_DOCUMENTS."""
    return max(0, (MAX_DOCUMENTS - document_bound(cfg)) // 2)


def story_part(cfg: dict) -> dict:
    """Часть конфига, определяющая экономику истории (без безвредных преобразований). ``variants``
    со всеми "random" в неё не входит: такие конфиги дают те же эпизоды, что и до появления ключа."""
    out = {k: v for k, v in cfg.items() if k not in ("id_salt", "order_seed")}
    if all(v == RANDOM_VARIANT for v in out.get("variants", {}).values()):
        out.pop("variants", None)
    return out
