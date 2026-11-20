"""Базовые политики (адаптеры политик организаторов, раздел 06 ТЗ) и демо-политика.

Политика видит ТОЛЬКО наблюдения — не объект среды, не seed, не эталон и не оценщика:

    policy.begin(первое_наблюдение)
    action = policy.act(наблюдение)     # повторяется до конца эпизода

  random       — случайный исследователь с фиксированным seed;
  reconciler   — простой алгоритм сверки (наивен к часовым поясам и отвлекающим случаям);
  flag_all     — атака «пометить всё»: каждый документ каждым типом;
  oracle       — только для оценщика: отправляет верные выводы (получает их от оценщика);
  rule_auditor — демо: скриптовый аудитор, буквально применяющий policy.json, без эталона.
"""
from __future__ import annotations

import random
from collections import defaultdict
from decimal import Decimal

from .audit_rules import rule_audit
from .money import convert_minor

TABLES = ("ledger", "invoices", "payments", "fx")
TYPES = ("duplicate_invoice", "payment_allocation", "fx_rate", "period_cutoff")


class Policy:
    name = "base"

    def begin(self, obs: dict) -> None:
        self.first = obs
        self.episode_id = obs["episode_id"]

    def act(self, obs: dict) -> dict:  # pragma: no cover
        raise NotImplementedError

    def submit(self, findings):
        return {"type": "submit", "findings": {"episode_id": self.episode_id, "findings": findings[:30]}}


class _Reader(Policy):
    """Постранично читает таблицы, затем вызывает ``decide(открытые_записи)``."""

    tables = TABLES

    def begin(self, obs):
        super().begin(obs)
        self.rows = {t: [] for t in TABLES}
        self.queue = list(self.tables)
        self.cursor = None
        self.pending = None

    def act(self, obs):
        if self.pending is not None:
            res = obs.get("result") or {}
            if obs.get("error") is None and res.get("table") == self.pending:
                self.rows[self.pending].extend(res["rows"])
                self.cursor = res["next_cursor"]
                if self.cursor is None:
                    self.queue.pop(0)
            else:
                self.queue.pop(0)
                self.cursor = None
            self.pending = None
        if self.queue and obs.get("steps_remaining", 1) > 1:
            self.pending = self.queue[0]
            return {"type": "list_records", "table": self.pending, "cursor": self.cursor, "limit": 50}
        public = dict(self.rows, manifest=self.first["manifest"], policy=self.first["policy"])
        return self.submit(self.decide(public))

    def decide(self, public):  # pragma: no cover
        raise NotImplementedError


class RuleAuditor(_Reader):
    name = "rule_auditor"

    def decide(self, public):
        return rule_audit(public)


class SimpleReconciler(_Reader):
    """Типичная первая попытка: сверка книги с документами по НАПЕЧАТАННОЙ дате, дубли — по
    клиенту и сумме, любая проводка 2900 — ошибка."""

    name = "reconciler"

    def decide(self, public):
        m = public["manifest"]
        base = m["base_currency"]
        p0, p1 = m["period_start"][:10], m["period_end"][:10]
        rates = {(f["source_currency"], f["date"]): (Decimal(f["rate"]), f["fx_id"]) for f in public["fx"]}
        inv_rows, pay_rows = defaultdict(list), defaultdict(list)
        for r in public["ledger"]:
            if r["payment_id"]:
                pay_rows[r["payment_id"]].append(r)
            elif r["invoice_id"]:
                inv_rows[r["invoice_id"]].append(r)
        out = []

        def add(t, aff, amt, ev):
            out.append({"type": t, "affected_ids": aff, "claimed_amount_minor": int(amt), "currency": base,
                        "evidence_ids": ev[:10], "explanation": "расхождение при сверке"})

        groups = defaultdict(list)
        for inv in public["invoices"]:
            groups[(inv["entity_id"], inv["currency"], inv["total_minor"])].append(inv)
        for g in groups.values():
            for a, b in zip(g, g[1:]):
                ar = next((r for r in inv_rows[b["invoice_id"]] if r["account"].startswith("1200")), None)
                if ar:
                    add("duplicate_invoice", [a["invoice_id"], b["invoice_id"]], ar["debit_minor"],
                        [a["doc_id"], b["doc_id"], ar["row_id"]])
        for kind, docs, rowsmap in (("inv", public["invoices"], inv_rows), ("pay", public["payments"], pay_rows)):
            for d in docs:
                ts = d["issued_at"] if kind == "inv" else d["timestamp"]
                amount = d["total_minor"] if kind == "inv" else d["amount_minor"]
                did = d["invoice_id"] if kind == "inv" else d["payment_id"]
                main = next((r for r in rowsmap[did] if r["account"].startswith("1200" if kind == "inv" else "1000")), None)
                if main is None:
                    continue
                posted = main["debit_minor"] - main["credit_minor"]
                day = ts[:10]
                if not (p0 <= day < p1):
                    add("period_cutoff", [did], abs(posted), [d["doc_id"], main["row_id"]])
                    continue
                if d["currency"] != base and (d["currency"], day) in rates:
                    rate, fid = rates[(d["currency"], day)]
                    exp = convert_minor(amount, d["currency"], base, rate)
                    if exp != posted:
                        add("fx_rate", [did], abs(posted - exp), [d["doc_id"], main["row_id"], fid])
                if kind == "pay":
                    for r in rowsmap[did]:
                        if r["account"].startswith("2900") and r["credit_minor"] > 0:
                            refs = d["invoice_refs"] or []
                            add("payment_allocation", [did] + [x["invoice_id"] for x in refs[:1]], r["credit_minor"],
                                [d["doc_id"], r["row_id"]])
        return out


class FlagAll(_Reader):
    """Атака: пометить каждый документ каждым типом (до 30 выводов)."""

    name = "flag_all"
    tables = ("invoices", "payments")

    def decide(self, public):
        base = public["manifest"]["base_currency"]
        docs = [(i["invoice_id"], i["doc_id"], i["total_minor"]) for i in public["invoices"]] + \
               [(p["payment_id"], p["doc_id"], p["amount_minor"]) for p in public["payments"]]
        all_ids = [d[1] for d in docs]
        out = [{"type": t, "affected_ids": [did], "claimed_amount_minor": abs(amt), "currency": base,
                "evidence_ids": [doc] + all_ids[:9], "explanation": "пометить всё"}
               for did, doc, amt in docs for t in TYPES]
        return out[:30]


class RandomExplorer(Policy):
    """Случайный исследователь с фиксированным seed: случайные действия, затем случайный ответ."""

    name = "random"

    def __init__(self, seed: int = 0):
        self.seed = seed

    def begin(self, obs):
        super().begin(obs)
        self.rng = random.Random("%d|%s" % (self.seed, obs["episode_id"]))
        self.budget = self.rng.randint(5, 30)
        self.n = 0
        self.ids = defaultdict(list)
        self.amounts = []
        self.cursors = defaultdict(list)

    def _observe(self, obs):
        res = obs.get("result") or {}
        rows = list(res.get("rows", []))
        for k in ("record", "document"):
            if k in res:
                rows.append(res[k])
        if res.get("next_cursor"):
            self.cursors[res["table"]].append(res["next_cursor"])
        for r in rows:
            for k in ("row_id", "doc_id", "invoice_id", "payment_id", "fx_id"):
                if r.get(k):
                    self.ids[k].append(r[k])
            for k in ("debit_minor", "credit_minor", "total_minor", "amount_minor"):
                if r.get(k):
                    self.amounts.append(abs(r[k]))

    def act(self, obs):
        self._observe(obs)
        self.n += 1
        r = self.rng
        if self.n > self.budget or obs.get("steps_remaining", 2) <= 1:
            findings = []
            if r.random() < 0.5:
                biz = self.ids["invoice_id"] + self.ids["payment_id"]
                ev = self.ids["row_id"] + self.ids["doc_id"] + self.ids["fx_id"]
                for _ in range(r.randint(1, 3)):
                    findings.append({"type": r.choice(TYPES),
                                     "affected_ids": r.sample(biz, min(len(biz), r.randint(1, 2))) or ["NONE"],
                                     "claimed_amount_minor": r.choice(self.amounts) if self.amounts else 0,
                                     "currency": self.first["manifest"]["base_currency"],
                                     "evidence_ids": r.sample(ev, min(len(ev), r.randint(1, 3))),
                                     "explanation": "случайная догадка"})
            return self.submit(findings)
        u = r.random()
        if u < 0.5:
            t = r.choice(TABLES)
            cur = r.choice(self.cursors[t]) if self.cursors[t] and r.random() < 0.5 else None
            return {"type": "list_records", "table": t, "cursor": cur, "limit": r.randint(1, 50)}
        if u < 0.7 and self.ids["row_id"]:
            return {"type": "get_record", "table": "ledger", "id": r.choice(self.ids["row_id"])}
        if u < 0.85:
            return {"type": "get_document", "doc_id": r.choice(self.ids["doc_id"]) if self.ids["doc_id"] else "DOC-UNKNOWN"}
        return {"type": "calculator", "expression": "%d*%d/100" % (r.randint(1, 10 ** 6), r.randint(1, 500))}


class Oracle(Policy):
    """Только для оценщика: отправляет на первом шаге выводы, полученные от оценщика."""

    name = "oracle"

    def __init__(self):
        self.findings = None

    def set_findings(self, payload):
        self.findings = payload

    def act(self, obs):
        return {"type": "submit", "findings": self.findings}


BASELINES = ("random", "reconciler", "flag_all", "oracle")
ALL_POLICIES = BASELINES + ("rule_auditor",)


def make_policy(name: str, seed: int = 0) -> Policy:
    return {"random": lambda: RandomExplorer(seed), "reconciler": SimpleReconciler, "flag_all": FlagAll,
            "oracle": Oracle, "rule_auditor": RuleAuditor}[name]()
