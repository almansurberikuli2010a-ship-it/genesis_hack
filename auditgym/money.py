"""Деньги, курсы валют и время — сторона ГЕНЕРАТОРА.

Все суммы хранятся как целые минимальные единицы (центы, тиыны, иены). Курсы — Decimal,
в файлах — десятичные строки. Правило конвертации (опубликовано в policy.json):

    base_minor = ROUND_HALF_EVEN(amount_minor × rate × 10^(exp_base − exp_src))

где rate — курс из fx.csv «валюта документа → базовая валюта» на календарную дату документа
в часовом поясе компании. Конвертация в ту же валюту идёт по курсу 1.

ВАЖНО: независимый проверяющий (audit_rules.py) этот модуль намеренно НЕ импортирует —
иначе ошибка здесь была бы «согласована» обеими сторонами и осталась бы незамеченной.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, getcontext

getcontext().prec = 34

# Показатели разрядности валют (ISO 4217). Компании вымышленные, коды валют настоящие.
CURRENCY_EXPONENT = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "KZT": 2,
    "CHF": 2,
    "JPY": 0,
    "KWD": 3,
}

# Ориентировочная стоимость 1 единицы валюты в USD — только для засева синтетических курсов.
REF_USD_VALUE = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"),
    "KZT": Decimal("0.00205"),
    "CHF": Decimal("1.12"),
    "JPY": Decimal("0.0067"),
    "KWD": Decimal("3.25"),
}


def exponent(currency: str) -> int:
    return CURRENCY_EXPONENT[currency]


def round_half_even(x: Decimal) -> int:
    """Банковское округление до целого."""
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def convert_minor(amount_minor: int, src: str, base: str, rate: Decimal) -> int:
    """Перевести целые минимальные единицы валюты ``src`` в минимальные единицы ``base``."""
    if src == base:
        return int(amount_minor)
    scale = Decimal(10) ** (exponent(base) - exponent(src))
    return round_half_even(Decimal(int(amount_minor)) * Decimal(rate) * scale)


def sig_round(x: Decimal, digits: int = 6) -> Decimal:
    """Округлить до ``digits`` значащих цифр (синтетические курсы, «ручное» округление курса)."""
    if x == 0:
        return Decimal(0)
    q = Decimal(1).scaleb(x.adjusted() - digits + 1)
    return x.quantize(q, rounding=ROUND_HALF_EVEN)


def rate_str(x: Decimal) -> str:
    """Курс как десятичная строка без экспоненты и лишних нулей."""
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def amount_tolerance(expected_minor: int) -> int:
    """Опубликованный допуск сопоставления: max(1, floor(|expected| × 0.0005))."""
    return max(1, abs(int(expected_minor)) * 5 // 10000)


# ----------------------------------------------------------------- время

def tz_from_offset(offset: str) -> timezone:
    """'+05:00' → часовой пояс UTC+5."""
    sign = 1 if offset[0] == "+" else -1
    hh, mm = offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("метка времени без смещения: %r" % s)
    return dt


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def local_date(ts: str | datetime, company_offset: str) -> date:
    """Календарная дата момента ``ts`` в часовом поясе компании."""
    dt = parse_ts(ts) if isinstance(ts, str) else ts
    return dt.astimezone(tz_from_offset(company_offset)).date()


def in_period(ts: str | datetime, period_start: str, period_end: str) -> bool:
    """Период — полуинтервал моментов времени [start, end)."""
    dt = parse_ts(ts) if isinstance(ts, str) else ts
    return parse_ts(period_start) <= dt < parse_ts(period_end)


def fx_id(d: date | str, src: str, tgt: str) -> str:
    """Идентификатор строки fx.csv (в файле нет столбца ID, он выводится из ключа строки)."""
    ds = d if isinstance(d, str) else d.isoformat()
    return "FX-%s-%s-%s" % (ds, src, tgt)
