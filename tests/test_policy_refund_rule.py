"""Правило возврата и переплаты в опубликованной политике (policy.json).

1. posting_rules сгенерированного эпизода описывают возврат, закрывающий счёт в ноль, и переплату;
2. policy_version = 3.3 в сгенерированном эпизоде и во всех открытых эпизодах;
5. независимая проводка возвратов только по тексту posting_rules (без кода проводок проекта)
   совпадает с книгой.
"""
import csv
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import judge_adapter as J

ROOT = Path(__file__).resolve().parent.parent
Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}
NI = {"partial_fx_boundary": 0, "masked_duplicate": 0}
SEEDS = range(4102, 4114)
TMP = None
EPS = {}


def _refund_cfg():
    from auditgym.config import DISTRACTORS
    return {"errors": dict(Z), "interactions": dict(NI), "distractors": dict({k: 0 for k in DISTRACTORS}, refund=2)}


def setUpModule():
    global TMP
    TMP = Path(tempfile.mkdtemp(prefix="auditgym-refund-"))
    cfg = _refund_cfg()
    for s in SEEDS:
        EPS[s] = TMP / str(s)
        J.generate_episode(s, cfg, str(EPS[s]))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def _policy(d):
    return json.loads((Path(d) / "policy.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ сценарии 1–2

class PolicyText(unittest.TestCase):
    def test_s1_refund_and_overpayment_rules_published(self):
        rules = _policy(EPS[SEEDS[0]])["posting_rules"]
        refund = rules.get("refund", "")
        for word in ("в ноль", "остаток 1200", "7800"):
            self.assertIn(word, refund, "posting_rules.refund: нет «%s»" % word)
        self.assertTrue(any("переплат" in str(v) for v in rules.values()),
                        "в posting_rules нет правила переплаты")

    def test_s2_policy_version(self):
        self.assertEqual(_policy(EPS[SEEDS[0]])["policy_version"], "3.3")
        files = sorted(ROOT.glob("episodes/*/public/*/policy.json"))
        self.assertTrue(files)
        for f in files:
            self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["policy_version"], "3.3", str(f))


# ------------------------------------------------------------------ сценарий 5

def _ts(s):
    return datetime.fromisoformat(s)


def _tz(offset):
    sign = -1 if offset.startswith("-") else 1
    h, m = offset.lstrip("+-").split(":")
    return timezone(sign * timedelta(hours=int(h), minutes=int(m)))


class IndependentBook:
    """Проводка документов эпизода по тексту posting_rules (policy.json) и fx.policy."""

    def __init__(self, d):
        d = Path(d)
        self.man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        self.policy = _policy(d)
        self.base = self.man["base_currency"]
        self.exp = self.man["currency_exponents"]
        self.tz = _tz(self.man["timezone"])
        self.start, self.end = _ts(self.man["period_start"]), _ts(self.man["period_end"])
        self.rates = {}
        with (d / "fx.csv").open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r["target_currency"] == self.base:
                    self.rates[(r["date"], r["source_currency"])] = Decimal(r["rate"])
        rd = lambda name: [json.loads(x) for x in (d / name).read_text(encoding="utf-8").splitlines() if x.strip()]
        self.invoices = {i["invoice_id"]: i for i in rd("invoices.jsonl")}
        self.payments = rd("payments.jsonl")
        with (d / "ledger.csv").open(encoding="utf-8", newline="") as f:
            self.ledger = list(csv.DictReader(f))
        refund_rule = self.policy["posting_rules"].get("refund", "")
        # Возврат по тексту правила: закрывающий счёт в ноль списывает остаток 1200 целиком
        # (если так написано), иначе — Дт 1200 по курсу, по которому счёт поставлен.
        self.refund_writes_off = "в ноль" in refund_rule and "остаток" in refund_rule

    def in_period(self, s):
        return self.start <= _ts(s) < self.end

    def rate(self, cur, moment):
        if cur == self.base:
            return Decimal(1)
        return self.rates[(_ts(moment).astimezone(self.tz).date().isoformat(), cur)]

    def conv(self, amount, cur, rate):
        q = Decimal(amount) * rate * (Decimal(10) ** (self.exp[self.base] - self.exp[cur]))
        return int(q.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))

    def refunds(self):
        """[(платёж, {invoice_id: (Дт 1200 по правилам, Дт 1200 по курсу счёта, Дт 1200 остатком)}, нетто Кт 7800)]
        по каждому возврату."""
        events = []
        for i in self.invoices.values():
            if self.in_period(i["issued_at"]):
                events.append((_ts(i["issued_at"]), 0, i["doc_id"], "inv", i))
        for p in self.payments:
            if self.in_period(p["timestamp"]) and p["invoice_refs"] is not None:
                events.append((_ts(p["timestamp"]), 1, p["doc_id"], "pay", p))
        events.sort(key=lambda e: e[:3])
        open_src, bal, booked = {}, {}, {}
        out = []
        for _, _, _, kind, doc in events:
            if kind == "inv":
                r = self.rate(doc["currency"], doc["issued_at"])
                k = doc["invoice_id"]
                open_src[k], bal[k], booked[k] = doc["total_minor"], self.conv(doc["total_minor"], doc["currency"], r), r
                continue
            cash = self.conv(doc["amount_minor"], doc["currency"], self.rate(doc["currency"], doc["timestamp"]))
            is_refund = doc["amount_minor"] < 0
            lines, credits = {}, 0
            for ref in doc["invoice_refs"]:
                k, a = ref["invoice_id"], ref["amount_minor"]
                inv = self.invoices[k]
                naive = self.conv(a, inv["currency"], booked[k])
                open_src[k] -= a
                closes = open_src[k] == 0
                writeoff = bal[k] if closes else naive
                c = writeoff if (not is_refund or self.refund_writes_off) else naive
                bal[k] -= c
                credits += c
                lines[k] = (-c, -naive, -writeoff)
            if is_refund:
                out.append((doc, lines, cash - credits))
        return out


class IndependentRefundPosting(unittest.TestCase):
    def test_s5_refund_rows_match_rules_text(self):
        checked = differs = 0
        for s in SEEDS:
            book = IndependentBook(EPS[s])
            for pay, lines, fx in book.refunds():
                pid = pay["payment_id"]
                rows = [r for r in book.ledger if r["payment_id"] == pid]
                for inv_id, (dr, naive, writeoff) in lines.items():
                    ar = [r for r in rows if r["account"].startswith("1200") and r["invoice_id"] == inv_id]
                    got = sum(int(r["debit_minor"]) - int(r["credit_minor"]) for r in ar)
                    self.assertEqual(got, dr, "seed %d, %s, %s: строка 1200" % (s, pid, inv_id))
                    differs += naive != writeoff
                got_fx = sum(int(r["credit_minor"]) - int(r["debit_minor"]) for r in rows if r["account"].startswith("7800"))
                self.assertEqual(got_fx, fx, "seed %d, %s: строка 7800" % (s, pid))
                checked += 1
        self.assertGreater(checked, 0, "в эпизодах нет возвратов")
        self.assertGreater(differs, 0, "нет возврата, где остаток 1200 отличается от пересчёта по курсу")


if __name__ == "__main__":
    unittest.main()
