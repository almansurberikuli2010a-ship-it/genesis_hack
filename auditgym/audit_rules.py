"""НЕЗАВИСИМЫЙ ПРОВЕРЯЮЩИЙ: рассуждения только по ОТКРЫТЫМ записям и опубликованной политике.

Модуль намеренно не импортирует код денег и времени генератора (money.py). Конвертация
считается на точных дробях (fractions.Fraction) с явным банковским округлением, локальная
дата и границы периода — ручной арифметикой смещений UTC, разрядности валют берутся из
manifest, счета учёта — по коду. Иначе ошибка в общей функции была бы «согласована» обеими
сторонами и прошла бы незамеченной (это показал мутационный тест).

Три назначения, ни одно не использует truth.json:
  * book_invariants   — бухгалтерские равенства (книги до и после внесения ошибок);
  * rule_audit        — эталонный аудитор, буквально применяющий policy.json;
  * verify_evidence   — пересчёт ошибки ТОЛЬКО по записям одного набора доказательств.
"""
from __future__ import annotations

import re
from collections import defaultdict
from fractions import Fraction

CASH, AR, UNAPPLIED, REVENUE, FXACC = "1000", "1200", "2900", "4000", "7800"

_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)([+-])(\d\d):(\d\d)$")


def code(account: str) -> str:
    """Код счёта учёта: '1200 Дебиторская задолженность' → '1200'."""
    return account.split(" ", 1)[0]


# ------------------------------------------------------------------ независимая арифметика

def _days_from_civil(y, m, d):
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _civil_from_days(z):
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    return y + (m <= 2), m, d


def _offset_seconds(off: str) -> int:
    sign = 1 if off[0] == "+" else -1
    return sign * (int(off[1:3]) * 3600 + int(off[4:6]) * 60)


def epoch_seconds(ts: str) -> int:
    mt = _TS.match(ts)
    if not mt:
        raise ValueError("метка времени не ISO-8601 со смещением: %r" % ts)
    y, mo, d, hh, mi, ss = (int(mt.group(i)) for i in range(1, 7))
    local = _days_from_civil(y, mo, d) * 86400 + hh * 3600 + mi * 60 + ss
    return local - _offset_seconds(mt.group(7) + mt.group(8) + ":" + mt.group(9))


def company_date(ts: str, company_offset: str) -> str:
    y, m, d = _civil_from_days((epoch_seconds(ts) + _offset_seconds(company_offset)) // 86400)
    return "%04d-%02d-%02d" % (y, m, d)


def within(ts: str, start: str, end: str) -> bool:
    return epoch_seconds(start) <= epoch_seconds(ts) < epoch_seconds(end)


def policy_convert(amount: int, src: str, base: str, rate: str, exps: dict) -> int:
    """ROUND_HALF_EVEN(amount × rate × 10^(exp_base − exp_src)) на точных дробях."""
    if src == base:
        return int(amount)
    v = Fraction(int(amount)) * Fraction(rate) * Fraction(10) ** (exps[base] - exps[src])
    n = v.numerator // v.denominator  # floor
    rem = v - n
    if rem > Fraction(1, 2) or (rem == Fraction(1, 2) and n % 2 == 1):
        n += 1
    return n


# ------------------------------------------------------------------ индексы книги

class Books:
    def __init__(self, public: dict):
        self.m = public["manifest"]
        self.base = self.m["base_currency"]
        self.tz = self.m["timezone"]
        self.exps = self.m["currency_exponents"]
        self.ledger = public["ledger"]
        self.invoices = public["invoices"]
        self.payments = public["payments"]
        self.rates = {(f["source_currency"], f["date"]): f["rate"] for f in public["fx"]}
        self.fx_ids = {(f["source_currency"], f["date"]): f["fx_id"] for f in public["fx"]}
        self.inv_rows = defaultdict(list)  # проводки счетов (без payment_id)
        self.pay_rows = defaultdict(list)  # проводки платежей
        self.op_rows = defaultdict(list)   # операционные проводки без документов, по моменту
        for r in self.ledger:
            if r["payment_id"] is not None:
                self.pay_rows[r["payment_id"]].append(r)
            elif r["invoice_id"] is not None:
                self.inv_rows[r["invoice_id"]].append(r)
            else:
                self.op_rows[r["timestamp"]].append(r)

    def rate_date(self, ts: str) -> str:
        return company_date(ts, self.tz)

    def conv(self, amount: int, cur: str, ts: str):
        if cur == self.base:
            return amount
        key = (cur, self.rate_date(ts))
        if key not in self.rates:
            return None
        return policy_convert(amount, cur, self.base, self.rates[key], self.exps)

    def in_period(self, ts: str) -> bool:
        return within(ts, self.m["period_start"], self.m["period_end"])


def _signed(r):
    return r["debit_minor"] - r["credit_minor"]


# ------------------------------------------------------------------ равенства

def posting_breaks(public: dict) -> list:
    """Документы, чей статус проводки противоречит периоду: в периоде, но не проведён, либо вне
    периода, но проведён. Внесённые ошибки period_cutoff — ровно второй случай."""
    b = Books(public)
    out = []
    for i in b.invoices:
        if bool(b.inv_rows.get(i["invoice_id"])) != b.in_period(i["issued_at"]):
            out.append(("invoice", i["invoice_id"]))
    for p in b.payments:
        if bool(b.pay_rows.get(p["payment_id"])) != b.in_period(p["timestamp"]):
            out.append(("payment", p["payment_id"]))
    return out


def unsettled_invoices(public: dict) -> set:
    """Счета, полностью расписанные платежами этого периода, но с ненулевым сальдо 1200."""
    b = Books(public)
    paid = defaultdict(int)
    for p in b.payments:
        if b.in_period(p["timestamp"]):
            for x in p["invoice_refs"] or []:
                paid[x["invoice_id"]] += x["amount_minor"]
    bal = defaultdict(int)
    for r in b.ledger:
        if code(r["account"]) == AR:
            bal[r["invoice_id"]] += _signed(r)
    return {i["invoice_id"] for i in b.invoices if paid[i["invoice_id"]] == i["total_minor"] and bal[i["invoice_id"]] != 0}


def book_invariants(public: dict, subledger: bool = True) -> list[str]:
    """Нарушенные бухгалтерские равенства (пустой список — книги внутренне согласованы).
    ``subledger=False`` пропускает равенства, которые внесённые ошибки ОБЯЗАНЫ нарушать (сальдо
    1200 расписанных счетов и «проведён ⇔ в периоде»); их объявленность проверяет datachecks."""
    b = Books(public)
    v = []
    dr = sum(r["debit_minor"] for r in b.ledger)
    cr = sum(r["credit_minor"] for r in b.ledger)
    if dr != cr:
        v.append("оборотно-сальдовый баланс: дебет %d ≠ кредит %d" % (dr, cr))
    for r in b.ledger:
        if not b.in_period(r["timestamp"]):
            v.append("строка %s вне периода" % r["row_id"])
        if r["debit_minor"] < 0 or r["credit_minor"] < 0 or (r["debit_minor"] and r["credit_minor"]):
            v.append("строка %s: недопустимые дебет/кредит" % r["row_id"])
        if r["currency"] != b.base:
            v.append("строка %s не в базовой валюте" % r["row_id"])
    for key, rows in list(b.inv_rows.items()) + list(b.pay_rows.items()) + list(b.op_rows.items()):
        if sum(_signed(r) for r in rows) != 0:
            v.append("проводка %s не сбалансирована" % key)
    for rows in b.op_rows.values():
        for r in rows:
            if code(r["account"]) not in (CASH,) and not code(r["account"]).startswith("6"):
                v.append("операционная строка %s на счёте %s" % (r["row_id"], r["account"]))
    for p in b.payments:
        if p["invoice_refs"] is not None and sum(x["amount_minor"] for x in p["invoice_refs"]) != p["amount_minor"]:
            v.append("платёж %s: расписка не равна сумме" % p["payment_id"])
    inv_ids = {i["invoice_id"] for i in b.invoices}
    for i in b.invoices:
        if sum(l["amount_minor"] for l in i["line_items"]) != i["total_minor"]:
            v.append("счёт %s: строки не дают итог" % i["invoice_id"])
    for r in b.ledger:
        if r["invoice_id"] is not None and r["invoice_id"] not in inv_ids:
            v.append("строка %s ссылается на неизвестный счёт" % r["row_id"])
    if subledger:
        for inv in sorted(unsettled_invoices(public)):
            v.append("счёт %s расписан полностью, но сальдо 1200 не нулевое" % inv)
        for kind, key in posting_breaks(public):
            v.append("%s %s: статус проводки противоречит периоду" % (kind, key))
    return v


# ------------------------------------------------------------------ независимая перепроводка

def expected_postings(public: dict) -> tuple[dict, list[str]]:
    """Заново провести каждый документ периода по опубликованной политике (точные дроби).
    → ({("inv"|"pay", id): отсортированные строки (счёт, дебет, кредит, invoice_id)}, проблемы)."""
    b = Books(public)
    events = [(epoch_seconds(i["issued_at"]), 0, i["invoice_id"], "inv", i) for i in b.invoices if b.in_period(i["issued_at"])]
    events += [(epoch_seconds(p["timestamp"]), 1, p["payment_id"], "pay", p) for p in b.payments if b.in_period(p["timestamp"])]
    events.sort(key=lambda e: e[:3])
    exp, problems = {}, []
    ar_open, ar_bal, inv_doc = {}, {}, {}
    for _, _, key, kind, d in events:
        if kind == "inv":
            amt = b.conv(d["total_minor"], d["currency"], d["issued_at"])
            if amt is None:
                problems.append("нет курса для счёта %s" % key)
                continue
            exp[("inv", key)] = sorted([(AR, amt, 0, key), (REVENUE, 0, amt, key)])
            ar_open[key], ar_bal[key], inv_doc[key] = d["total_minor"], amt, d
            continue
        cash = b.conv(d["amount_minor"], d["currency"], d["timestamp"])
        if cash is None:
            problems.append("нет курса для платежа %s" % key)
            continue
        credits, susp = [], 0
        if d["invoice_refs"] is None:
            susp = cash
        else:
            for x in d["invoice_refs"]:
                ik, a = x["invoice_id"], x["amount_minor"]
                if ik not in ar_open:
                    problems.append("платёж %s ссылается на счёт %s, не проведённый в периоде" % (key, ik))
                    continue
                ar_open[ik] -= a
                inv = inv_doc[ik]
                c = ar_bal[ik] if ar_open[ik] == 0 else b.conv(a, inv["currency"], inv["issued_at"])
                ar_bal[ik] -= c
                credits.append((ik, c))
        link = credits[0][0] if d["invoice_refs"] is not None and len(d["invoice_refs"]) == 1 else None
        rows = [(CASH, max(cash, 0), max(-cash, 0), link)]
        rows += [(AR, max(-c, 0), max(c, 0), ik) for ik, c in credits]
        if susp:
            rows.append((UNAPPLIED, max(-susp, 0), max(susp, 0), None))
        fx = cash - sum(c for _, c in credits) - susp
        if fx:
            rows.append((FXACC, max(-fx, 0), max(fx, 0), link))
        exp[("pay", key)] = sorted(rows)
    return exp, problems


def actual_postings(public: dict) -> dict:
    b = Books(public)
    act = {("inv", k): sorted((code(r["account"]), r["debit_minor"], r["credit_minor"], r["invoice_id"]) for r in rows)
           for k, rows in b.inv_rows.items()}
    act.update({("pay", k): sorted((code(r["account"]), r["debit_minor"], r["credit_minor"], r["invoice_id"]) for r in rows)
                for k, rows in b.pay_rows.items()})
    return act


def repost_differences(public: dict, allowed: set = frozenset()) -> list[str]:
    """Проводки, не совпадающие с перепроводкой по политике, кроме документов из ``allowed``
    (затронутых объявленными ошибками). Для чистых книг ``allowed`` пусто: книга обязана
    совпасть с политикой полностью."""
    exp, problems = expected_postings(public)
    act = actual_postings(public)
    out = [p for p in problems if not any(a in p for a in allowed)]
    for k in sorted(set(exp) | set(act)):
        if k[1] in allowed:
            continue
        if exp.get(k) != act.get(k):
            out.append("%s %s: проводка не совпадает с политикой (ожидалось %s, в книге %s)" % (
                k[0], k[1], exp.get(k), act.get(k)))
    return out


# ------------------------------------------------------------------ эталонный аудитор

def _main_row(rows, acc):
    return next((r for r in rows if code(r["account"]) == acc), None)


def rule_audit(public: dict) -> list[dict]:
    """Эталонный аудитор: буквально применяет опубликованную политику к открытым записям."""
    b = Books(public)
    findings = []

    def add(t, affected, amount, evidence, why):
        findings.append({"type": t, "affected_ids": sorted(affected), "claimed_amount_minor": int(amount),
                         "currency": b.base, "evidence_ids": evidence, "explanation": why})

    for inv in b.invoices:
        ar = _main_row(b.inv_rows.get(inv["invoice_id"], []), AR)
        if ar is None:
            continue
        if not b.in_period(inv["issued_at"]):
            add("period_cutoff", [inv["invoice_id"]], ar["debit_minor"], [inv["doc_id"], ar["row_id"]],
                "счёт вне периода проведён в периоде")
            continue
        exp = b.conv(inv["total_minor"], inv["currency"], inv["issued_at"])
        if exp is not None and exp != ar["debit_minor"]:
            ev = [inv["doc_id"], ar["row_id"]]
            if inv["currency"] != b.base:
                ev.append(b.fx_ids[(inv["currency"], b.rate_date(inv["issued_at"]))])
            add("fx_rate", [inv["invoice_id"]], abs(ar["debit_minor"] - exp), ev, "пересчёт не по курсу политики")
    groups = defaultdict(list)
    for inv in b.invoices:
        groups[(inv["entity_id"], inv["currency"], epoch_seconds(inv["issued_at"]), inv["total_minor"],
                repr(inv["line_items"]))].append(inv)
    for g in groups.values():
        if len(g) < 2:
            continue
        g = sorted(g, key=lambda i: i["invoice_id"])
        for dup in g[1:]:
            ar = _main_row(b.inv_rows.get(dup["invoice_id"], []), AR)
            if ar is not None:
                add("duplicate_invoice", [g[0]["invoice_id"], dup["invoice_id"]], ar["debit_minor"],
                    [g[0]["doc_id"], dup["doc_id"], ar["row_id"]], "один и тот же счёт внесён дважды")
    for p in b.payments:
        rows = b.pay_rows.get(p["payment_id"], [])
        cash = _main_row(rows, CASH)
        if cash is None:
            continue
        if not b.in_period(p["timestamp"]):
            add("period_cutoff", [p["payment_id"]], abs(_signed(cash)), [p["doc_id"], cash["row_id"]],
                "платёж вне периода проведён в периоде")
            continue
        exp = b.conv(p["amount_minor"], p["currency"], p["timestamp"])
        if exp is not None and exp != _signed(cash):
            ev = [p["doc_id"], cash["row_id"]]
            if p["currency"] != b.base:
                ev.append(b.fx_ids[(p["currency"], b.rate_date(p["timestamp"]))])
            add("fx_rate", [p["payment_id"]], abs(_signed(cash) - exp), ev, "пересчёт не по курсу политики")
        refs = p["invoice_refs"]
        if refs is None:
            continue
        ref_ids = [x["invoice_id"] for x in refs]
        credited = {r["invoice_id"] for r in rows if code(r["account"]) == AR}
        missing = [i for i in ref_ids if i not in credited] or ref_ids[:1]
        for r in rows:
            if code(r["account"]) == UNAPPLIED and r["credit_minor"] > 0:
                add("payment_allocation", [p["payment_id"], ref_ids[0]], r["credit_minor"], [p["doc_id"], r["row_id"]],
                    "расписанная сумма осталась на 2900")
            if code(r["account"]) == AR and r["invoice_id"] not in ref_ids:
                add("payment_allocation", [p["payment_id"], missing[0]], abs(_signed(r)), [p["doc_id"], r["row_id"]],
                    "зачтено на счёт, которого нет в расписке")
    return findings


# ------------------------------------------------------------------ достаточность доказательств

def _lookup(public: dict) -> dict:
    idx = {}
    for r in public["ledger"]:
        idx[r["row_id"]] = ("ledger", r)
    for i in public["invoices"]:
        idx[i["doc_id"]] = ("invoice", i)
    for p in public["payments"]:
        idx[p["doc_id"]] = ("payment", p)
    for f in public["fx"]:
        idx[f["fx_id"]] = ("fx", f)
    return idx


def verify_evidence(issue: dict, public: dict) -> list[str]:
    """Проверить КАЖДЫЙ набор доказательств ошибки, используя только названные в нём записи
    (и manifest). Пустой список — каждый набор сам по себе доказывает тип, ID и сумму."""
    idx = _lookup(public)
    m = public["manifest"]
    problems = []
    for es in issue["evidence_sets"]:
        recs = [idx.get(e) for e in es]
        if any(x is None for x in recs):
            problems.append("ID доказательства нет в открытых записях: %s" % es)
            continue
        by = defaultdict(list)
        for kind, rec in recs:
            by[kind].append(rec)
        err = _verify_set(issue, by, m)
        if err:
            problems.append("%s: %s" % (es, err))
    return problems


def _verify_set(issue, by, m):
    t, exp, base = issue["type"], issue["expected_amount_minor"], m["base_currency"]
    if len(by["ledger"]) != 1:
        return "нужна ровно одна строка книги"
    row = by["ledger"][0]
    if t == "duplicate_invoice":
        a, b2 = (by["invoice"] + [None, None])[:2]
        if a is None or b2 is None:
            return "нужны два счёта"
        same = all(a[k] == b2[k] for k in ("entity_id", "currency", "total_minor", "line_items")) and \
            epoch_seconds(a["issued_at"]) == epoch_seconds(b2["issued_at"])
        if not same or a["invoice_id"] == b2["invoice_id"]:
            return "счета не являются дублями"
        if sorted([a["invoice_id"], b2["invoice_id"]]) != sorted(issue["affected_ids"]):
            return "affected_ids не совпадают с документами"
        if row["invoice_id"] not in (a["invoice_id"], b2["invoice_id"]) or code(row["account"]) not in (AR, REVENUE):
            return "строка не является проводкой счёта"
        return None if row["debit_minor"] + row["credit_minor"] == exp else "сумма не совпадает"
    if t == "payment_allocation":
        if len(by["payment"]) != 1:
            return "нужен документ платежа"
        p = by["payment"][0]
        refs = p["invoice_refs"]
        if refs is None or sum(x["amount_minor"] for x in refs) != p["amount_minor"]:
            return "платёж расписан не полностью"
        ref_ids = [x["invoice_id"] for x in refs]
        if row["payment_id"] != p["payment_id"]:
            return "строка не из этого платежа"
        bad = (code(row["account"]) == UNAPPLIED and row["credit_minor"] > 0) or \
              (code(row["account"]) == AR and row["invoice_id"] not in ref_ids)
        if not bad:
            return "строка — корректное распределение"
        if sorted(issue["affected_ids"]) != sorted([p["payment_id"], ref_ids[0]]):
            return "affected_ids не совпадают"
        return None if row["credit_minor"] == exp else "сумма не совпадает"
    kind = "invoice" if by["invoice"] else "payment"
    if len(by[kind]) != 1:
        return "нужен один исходный документ"
    doc = by[kind][0]
    ts = doc["issued_at"] if kind == "invoice" else doc["timestamp"]
    amount = doc["total_minor"] if kind == "invoice" else doc["amount_minor"]
    own = (row["invoice_id"] == doc["invoice_id"] and row["payment_id"] is None) if kind == "invoice" \
        else row["payment_id"] == doc["payment_id"]
    if not own:
        return "строка не принадлежит документу"
    posted = row["debit_minor"] + row["credit_minor"]
    if t == "period_cutoff":
        if within(ts, m["period_start"], m["period_end"]):
            return "документ внутри периода"
        if not within(row["timestamp"], m["period_start"], m["period_end"]):
            return "строка не внутри периода"
        return None if posted == exp else "сумма не совпадает"
    if t == "fx_rate":
        if len(by["fx"]) != 1:
            return "нужна строка курса"
        f = by["fx"][0]
        if f["source_currency"] != doc["currency"] or f["target_currency"] != base:
            return "валюты курса не совпадают"
        if f["date"] != company_date(ts, m["timezone"]):
            return "курс не на дату по политике"
        correct = policy_convert(amount, doc["currency"], base, f["rate"], m["currency_exponents"])
        diff = abs(posted - abs(correct))
        if diff == 0:
            return "проводка равна пересчёту по политике"
        return None if diff == exp else "сумма не совпадает (%d ≠ %d)" % (diff, exp)
    return "неизвестный тип"
