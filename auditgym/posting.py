"""Шаг 2 генерации: двойная запись по опубликованной политике.

Одна функция ``post`` строит и чистую книгу (без переопределений), и книгу с ошибками
(с переопределениями из injection.py) — поэтому любое отличие между ними вызвано только
записанным внесением.

Правила (они же в policy.json):
  * счёт:    Дт 1200 / Кт 4000 по курсу даты счёта;
  * платёж:  Дт 1000 по курсу даты платежа; Кт 1200 по каждой строке расписки по курсу,
             по которому счёт был поставлен; платёж, закрывающий счёт в ноль, списывает
             остаток 1200 целиком; разница — на 7800;
  * возврат: отрицательный платёж — Кт 1000 / Дт 1200;
  * платёж без расписки — целиком на 2900 (корректно);
  * фоновые операции: Дт 6xxx / Кт 1000, без документов;
  * документы вне периода (posted = False) не проводятся;
  * строк книги с нулевыми дебетом и кредитом нет.

Переопределения (ключ ("inv"|"pay", key)):
  rate_date — курс другой даты; rate — явный (неверный) курс; unapplied — часть расписанной
  суммы оставлена на 2900; misapply — зачесть на другой счёт; post_ts — дата проводки.
"""
from __future__ import annotations

from .money import convert_minor, local_date

ACC_CASH = "1000 Денежные средства"
ACC_AR = "1200 Дебиторская задолженность"
ACC_UNAPPLIED = "2900 Невыясненные поступления"
ACC_REVENUE = "4000 Выручка"
ACC_FX = "7800 Курсовые разницы"

CHART_OF_ACCOUNTS = {
    "1000": "Денежные средства (базовая валюта)",
    "1200": "Дебиторская задолженность покупателей",
    "2900": "Невыясненные поступления (платёж без расписки или нераспределённый остаток)",
    "4000": "Выручка",
    "6100": "Аренда помещений",
    "6200": "Заработная плата",
    "6300": "Коммунальные услуги",
    "6400": "Банковские комиссии",
    "6500": "Хозяйственные расходы",
    "7800": "Курсовые разницы (прибыль — кредит, убыток — дебет)",
}


class PostingError(RuntimeError):
    pass


def post(b, invoices, payments, overrides, background=()) -> list[dict]:
    """Вернуть строки книги с внутренними ключами; ``b`` — StoryBuilder (база, курсы, пояс)."""
    invoices = [i for i in invoices if i["posted"]]
    payments = [p for p in payments if p["posted"]]
    events = []
    for inv in invoices:
        ov = overrides.get(("inv", inv["key"]), {})
        events.append((ov.get("post_ts", inv["issued"]), 0, inv["key"], "inv", inv))
    for p in payments:
        ov = overrides.get(("pay", p["key"]), {})
        events.append((ov.get("post_ts", p["ts"]), 1, p["key"], "pay", p))
    for g in background:
        events.append((g["ts"], 2, g["key"], "bg", g))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    inv_by_key = {i["key"]: i for i in invoices}
    ar_open, ar_bal, booked_rate = {}, {}, {}  # остаток в валюте счёта, сальдо 1200, курс постановки
    rows: list[dict] = []

    def row(ts, entry, account, dr, cr, ent, inv_k, pay_k):
        rows.append({"ts": ts.astimezone(b.tzc), "entry": entry, "account": account, "debit": int(dr),
                     "credit": int(cr), "entity": ent, "inv": inv_k, "pay": pay_k})

    for ts, _, key, kind, doc in events:
        ov = overrides.get((kind, key), {})
        if kind == "bg":
            row(ts, ("bg", key), doc["account"], doc["amount"], 0, None, None, None)
            row(ts, ("bg", key), ACC_CASH, 0, doc["amount"], None, None, None)
            continue
        if kind == "inv":
            rd = ov.get("rate_date", local_date(doc["issued"], b.offset))
            rate = ov["rate"] if "rate" in ov else b.rate(doc["currency"], rd)
            amt = convert_minor(doc["total"], doc["currency"], b.base, rate)
            row(ts, ("inv", key), ACC_AR, amt, 0, doc["entity"], key, None)
            row(ts, ("inv", key), ACC_REVENUE, 0, amt, doc["entity"], key, None)
            ar_open[key], ar_bal[key], booked_rate[key] = doc["total"], amt, rate
            continue
        rd = ov.get("rate_date", local_date(doc["ts"], b.offset))
        rate = ov["rate"] if "rate" in ov else b.rate(doc["currency"], rd)
        cash = convert_minor(doc["amount"], doc["currency"], b.base, rate)
        refs = doc["refs"]
        unapplied = ov.get("unapplied", 0)
        misapply = ov.get("misapply", {})
        credits = []
        if refs is None:
            susp = cash
        else:
            susp = convert_minor(unapplied, doc["currency"], b.base, rate) if unapplied else 0
            for i, (ik, amt_ref) in enumerate(refs):
                book = misapply.get(ik, ik)
                applied = amt_ref - (unapplied if i == 0 else 0)
                if book not in ar_open:
                    raise PostingError("платёж проведён раньше своего счёта")
                ar_open[book] -= applied
                if ar_open[book] == 0:
                    c = ar_bal[book]  # закрывающий платёж списывает остаток 1200 целиком
                else:
                    c = convert_minor(applied, inv_by_key[book]["currency"], b.base, booked_rate[book])
                ar_bal[book] -= c
                credits.append((book, c))
        link = credits[0][0] if refs is not None and len(refs) == 1 else None
        entry = ("pay", key)
        row(ts, entry, ACC_CASH, max(cash, 0), max(-cash, 0), doc["entity"], link, key)
        for book, c in credits:
            if c:  # строк с нулевой суммой нет (например, расписанная сумма целиком осталась на 2900)
                row(ts, entry, ACC_AR, max(-c, 0), max(c, 0), doc["entity"], book, key)
        if susp:
            row(ts, entry, ACC_UNAPPLIED, max(-susp, 0), max(susp, 0), doc["entity"], None, key)
        fx = cash - sum(c for _, c in credits) - susp
        if fx:
            row(ts, entry, ACC_FX, max(-fx, 0), max(fx, 0), doc["entity"], link, key)
    return rows
