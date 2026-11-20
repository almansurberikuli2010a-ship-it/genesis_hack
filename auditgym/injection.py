"""Шаг 3 генерации: внесение ошибок как ЗАПИСАННЫХ ПРИЧИННЫХ ИЗМЕНЕНИЙ.

Ошибка — либо переопределение проводки существующего документа, либо новый документ
(копия-дубль, документ соседнего периода). Каждое изменение получает mutation_id и
описание; эталон строится из этих записей, а не поиском «подозрительных строк».
Цели блокируются (locked_*), чтобы ошибки не пересекались случайно; сценарии
взаимодействия кладут два изменения на один документ намеренно.

Типы (формулировки ТЗ):
  duplicate_invoice  — дубли счетов;
  payment_allocation — пропущенное или частичное распределение платежа (и зачёт на чужой счёт);
  fx_rate            — неверная дата или курс конвертации (курс другой даты, округлённый,
                       перевёрнутый);
  period_cutoff      — ошибка отнесения к отчётному периоду (документ следующего периода
                       задним числом; документ прошлого периода проведён поздно).
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from decimal import Decimal

from . import money
from .config import ERROR_TYPES, INTERACTIONS
from .money import convert_minor, local_date, rate_str, sig_round, tz_from_offset
from .story import GenerationError


def inject(b) -> None:
    """Заполнить b.overrides, b.injected_invoices/payments, b.issues, b.mutations."""
    b.overrides, b.injected_invoices, b.injected_payments, b.issues, b.mutations = {}, [], [], [], []
    plan = []
    for t in ERROR_TYPES:
        plan += [t] * b.cfg["errors"][t]
    for t in INTERACTIONS:
        plan += [t] * b.cfg["interactions"][t]
    b.rng.shuffle(plan)
    plan.sort(key=lambda t: 0 if t in INTERACTIONS else 1)  # самые требовательные цели — первыми
    pairs = list(b.boundary_pairs)
    for n, t in enumerate(plan):
        mid = "M%02d" % (n + 1)
        ok = INJECTORS[t](b, mid, pairs)
        if not ok:
            raise GenerationError("не удалось разместить %s (эпизод мал для конфига)" % t)


def _issue(b, itype, affected, evidence, mid):
    b.issues.append({"type": itype, "affected": affected, "evidence": evidence, "mutation": mid})


def _mutation(b, **kw):
    b.mutations.append(kw)


def _pick(b, items, pred):
    cands = [x for x in items if pred(x)]
    return b.rng.choice(cands) if cands else None


# ------------------------------------------------------------------ дубль счёта

def inj_duplicate_invoice(b, mid, pairs):
    inv = _pick(b, b.invoices, lambda i: i["posted"] and i["key"] not in b.locked_inv)
    if not inv:
        return False
    dup = copy.deepcopy(inv)
    dup["key"] = b.next_key("inv")
    b.injected_invoices.append(dup)
    b.locked_inv.update({inv["key"], dup["key"]})
    _mutation(b, mutation_id=mid, kind="add_document", issue_type="duplicate_invoice",
              document=("inv", dup["key"]), copy_of=("inv", inv["key"]),
              description="счёт внесён повторно под новым номером (точная копия строк, суммы и даты)")
    _issue(b, "duplicate_invoice", [("inv", inv["key"]), ("inv", dup["key"])], ("duplicate", inv["key"], dup["key"]), mid)
    return True


# ------------------------------------------------------------------ распределение платежа

def inj_payment_allocation(b, mid, pairs):
    p = _pick(b, b.payments, lambda p: p["posted"] and p["refs"] is not None and len(p["refs"]) == 1
              and p["amount"] > 0 and p["key"] not in b.locked_pay and p["refs"][0][1] >= 10)
    if not p:
        return False
    amt = p["refs"][0][1]
    if b.rng.random() < 0.45:
        u, variant = amt, "missing"
        text = "платёж расписан на счёт, но не распределён: вся сумма осталась на 2900"
    else:
        u, variant = max(1, int(amt * Decimal(str(b.rng.uniform(0.2, 0.8))))), "partial"
        text = "платёж распределён частично: часть расписанной суммы осталась на 2900"
    b.overrides.setdefault(("pay", p["key"]), {})["unapplied"] = u
    b.locked_pay.add(p["key"])
    _mutation(b, mutation_id=mid, kind="booking_override", issue_type="payment_allocation", variant=variant,
              document=("pay", p["key"]), unapplied_minor=u, description=text)
    _issue(b, "payment_allocation", [("pay", p["key"]), ("inv", p["refs"][0][0])], ("unapplied", p["key"]), mid)
    return True


# ------------------------------------------------------------------ дата или курс конвертации

def _wrong_date(b, cur, true_d, amount):
    ks = [1, -1, 2, -2, 3, 5, 7, -3]
    b.rng.shuffle(ks)
    for k in ks:
        d = true_d + timedelta(days=k)
        if not (b.fx_dates[0] <= d <= b.fx_dates[1]):
            continue
        diff = abs(b.conv(amount, cur, d) - b.conv(amount, cur, true_d))
        if diff >= max(50, money.amount_tolerance(diff) * 4):
            return d, k
    return None, None


def inj_fx_rate(b, mid, pairs):
    r = b.rng
    targets = [("inv", i) for i in b.invoices
               if i["posted"] and i["currency"] != b.base and i["key"] not in b.locked_inv]
    targets += [("pay", p) for p in b.payments
                if p["posted"] and p["currency"] != b.base and p["key"] not in b.locked_pay and p["amount"] > 0]
    r.shuffle(targets)
    u = r.random()
    if u < 0.5:
        order = ["wrong_date", "rounded_rate", "inverted_rate"]
    elif u < 0.75:
        order = ["rounded_rate", "wrong_date", "inverted_rate"]
    else:
        order = ["inverted_rate", "wrong_date", "rounded_rate"]
    texts = {"wrong_date": "взят курс другой даты", "rounded_rate": "взят вручную округлённый курс",
             "inverted_rate": "курс применён «вверх ногами» (1 / курс)"}
    for kind, doc in targets:
        ts = doc["issued"] if kind == "inv" else doc["ts"]
        amount = doc["total"] if kind == "inv" else doc["amount"]
        true_d = local_date(ts, b.offset)
        true_rate = b.rate(doc["currency"], true_d)
        correct = convert_minor(amount, doc["currency"], b.base, true_rate)
        chosen = None
        for variant in order:
            if variant == "wrong_date":
                wd, k = _wrong_date(b, doc["currency"], true_d, amount)
                if wd is not None:
                    chosen = (variant, {"rate_date": wd}, b.rate(doc["currency"], wd),
                              {"rate_date_used": wd.isoformat(), "day_shift": k})
            else:
                if variant == "rounded_rate":
                    bad = sig_round(true_rate, 3)
                else:
                    if not (Decimal("0.2") <= true_rate <= Decimal(5)):
                        continue  # перевёрнутый курс правдоподобен только для пар около паритета
                    bad = sig_round(Decimal(1) / true_rate, 6)
                diff = abs(convert_minor(amount, doc["currency"], b.base, bad) - correct)
                if bad != true_rate and diff >= max(50, money.amount_tolerance(diff) * 4):
                    chosen = (variant, {"rate": bad}, bad, {})
            if chosen:
                break
        if chosen is None:
            continue
        variant, ov, used_rate, extra = chosen
        b.overrides.setdefault((kind, doc["key"]), {}).update(ov)
        (b.locked_inv if kind == "inv" else b.locked_pay).add(doc["key"])
        _mutation(b, mutation_id=mid, kind="booking_override", issue_type="fx_rate", variant=variant,
                  document=(kind, doc["key"]), rate_used=rate_str(used_rate), rate_policy=rate_str(true_rate),
                  rate_date_policy=true_d.isoformat(), description=texts[variant], **extra)
        _issue(b, "fx_rate", [(kind, doc["key"])], ("fx", kind, doc["key"]), mid)
        return True
    return False


# ------------------------------------------------------------------ отнесение к периоду

def _backdate_ts(b, not_before=None):
    r = b.rng
    ts = datetime(b.p_start.year, b.p_start.month, b.last_day, r.randint(9, 18), r.randint(0, 59),
                  r.randint(0, 59), tzinfo=b.tzc)
    if not_before is not None and ts <= not_before:
        ts = (not_before + timedelta(minutes=r.randint(1, 15))).astimezone(b.tzc)
    if ts >= b.p_end:
        raise GenerationError("нельзя провести задним числом внутри периода")
    return ts


def _frontdate_ts(b):
    r = b.rng
    return datetime(b.p_start.year, b.p_start.month, r.randint(1, 2), r.randint(9, 18), r.randint(0, 59),
                    r.randint(0, 59), tzinfo=b.tzc)


def inj_period_cutoff(b, mid, pairs):
    r = b.rng
    open_inv = []
    if r.random() < 0.4:  # оплата, полученная в следующем периоде, проведена задним числом
        paid = {ik for p in b.payments for ik, _ in (p["refs"] or [])}
        open_inv = [i for i in b.invoices if i["posted"] and i["key"] not in paid and i["key"] not in b.unremitted_inv
                    and i["key"] not in b.locked_inv and i["issued"] < b.p_end]
    if open_inv:
        inv = r.choice(open_inv)
        p = b.new_payment(inv["entity"], inv["currency"], b.display(b.next_period_instant(), "pay"), inv["total"],
                          [(inv["key"], inv["total"])], display=False)
        b.injected_payments.append(p)
        b.overrides[("pay", p["key"])] = {"post_ts": _backdate_ts(b, inv["issued"])}
        b.locked_inv.add(inv["key"])
        b.locked_pay.add(p["key"])
        _mutation(b, mutation_id=mid, kind="add_document_backdated", issue_type="period_cutoff", variant="next_period",
                  document=("pay", p["key"]), description="оплата следующего периода проведена задним числом")
        _issue(b, "period_cutoff", [("pay", p["key"])], ("cutoff", "pay", p["key"]), mid)
        return True
    ent = r.choice(b.customers)
    if r.random() < 0.4:
        inv = b.new_invoice(ent, b.prior_period_instant())
        post_ts, variant, text = _frontdate_ts(b), "prior_period", "счёт прошлого периода проведён поздно, в этом периоде"
    else:
        inv = b.new_invoice(ent, b.next_period_instant())
        post_ts, variant, text = _backdate_ts(b), "next_period", "счёт следующего периода проведён задним числом"
    b.injected_invoices.append(inv)
    b.overrides[("inv", inv["key"])] = {"post_ts": post_ts}
    b.locked_inv.add(inv["key"])
    _mutation(b, mutation_id=mid, kind="add_document_misdated_posting", issue_type="period_cutoff", variant=variant,
              document=("inv", inv["key"]), description=text)
    _issue(b, "period_cutoff", [("inv", inv["key"])], ("cutoff", "inv", inv["key"]), mid)
    return True


# ------------------------------------------------------------------ сценарии взаимодействия

def inj_partial_fx_boundary(b, mid, pairs):
    """Частичный платёж в другой валюте на границе периода: курс взят по дате UTC (fx_rate),
    часть расписанной суммы оставлена на 2900 (payment_allocation)."""
    if not pairs:
        return False
    inv, p = pairs.pop(0)
    true_d = local_date(p["ts"], b.offset)
    utc_d = p["ts"].astimezone(tz_from_offset("+00:00")).date()
    wd = utc_d if utc_d != true_d else true_d + timedelta(days=1)
    if abs(b.conv(p["amount"], p["currency"], wd) - b.conv(p["amount"], p["currency"], true_d)) < 1:
        return False
    amt = p["refs"][0][1]
    u = max(1, int(amt * Decimal(str(b.rng.uniform(0.25, 0.6)))))
    b.overrides[("pay", p["key"])] = {"rate_date": wd, "unapplied": u}
    b.locked_pay.add(p["key"])
    _mutation(b, mutation_id=mid, kind="booking_override", issue_type="interaction", scenario="partial_fx_boundary",
              document=("pay", p["key"]), rate_date_used=wd.isoformat(), rate_date_policy=true_d.isoformat(),
              unapplied_minor=u,
              description="частичный валютный платёж на границе периода: курс по дате UTC и неполное распределение")
    _issue(b, "fx_rate", [("pay", p["key"])], ("fx", "pay", p["key"]), mid)
    _issue(b, "payment_allocation", [("pay", p["key"]), ("inv", inv["key"])], ("unapplied", p["key"]), mid)
    return True


def _single_full_payments(b):
    by_inv = {}
    for p in b.payments:
        for ik, _ in (p["refs"] or []):
            by_inv.setdefault(ik, []).append(p)
    out = []
    for p in b.payments:
        if p["posted"] and p["refs"] and len(p["refs"]) == 1 and p["amount"] > 0:
            ik, a = p["refs"][0]
            inv = b.invoice(ik)
            if inv["posted"] and a == inv["total"] and len(by_inv[ik]) == 1:
                out.append((inv, p))
    return out


def inj_masked_duplicate(b, mid, pairs):
    """Дубль счёта, «замаскированный» оплатой: деньги за оригинал зачтены на дубль."""
    cands = [(i, p) for i, p in _single_full_payments(b) if i["key"] not in b.locked_inv and p["key"] not in b.locked_pay]
    if not cands:
        return False
    inv, p = b.rng.choice(cands)
    dup = copy.deepcopy(inv)
    dup["key"] = b.next_key("inv")
    b.injected_invoices.append(dup)
    b.overrides.setdefault(("pay", p["key"]), {})["misapply"] = {inv["key"]: dup["key"]}
    b.locked_inv.update({inv["key"], dup["key"]})
    b.locked_pay.add(p["key"])
    _mutation(b, mutation_id=mid, kind="add_document+booking_override", issue_type="interaction",
              scenario="masked_duplicate", document=("inv", dup["key"]), copy_of=("inv", inv["key"]),
              payment=("pay", p["key"]), applied_to=("inv", dup["key"]),
              description="дубль счёта, на который зачтена оплата оригинала: дубль выглядит оплаченным")
    _issue(b, "duplicate_invoice", [("inv", inv["key"]), ("inv", dup["key"])], ("duplicate", inv["key"], dup["key"]), mid)
    _issue(b, "payment_allocation", [("pay", p["key"]), ("inv", inv["key"])], ("misapplied", p["key"], dup["key"]), mid)
    return True


INJECTORS = {
    "duplicate_invoice": inj_duplicate_invoice,
    "payment_allocation": inj_payment_allocation,
    "fx_rate": inj_fx_rate,
    "period_cutoff": inj_period_cutoff,
    "partial_fx_boundary": inj_partial_fx_boundary,
    "masked_duplicate": inj_masked_duplicate,
}
