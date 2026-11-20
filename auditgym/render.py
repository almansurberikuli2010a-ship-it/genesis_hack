"""Шаг 4 генерации: публичные ID, порядок записей, открытые таблицы, эталон и provenance.

* ID — солёный хэш внутреннего ключа (``id_salt``); порядок записей — от ``order_seed``.
  Оба параметра — безвредные преобразования: экономика истории от них не зависит.
* Эталон (truth) вычисляется из записей о внесении и реально проведённых строк.
* provenance содержит полный ДИФФ книги «до → после» с причиной каждой изменённой строки
  (direct — сам изменённый документ; consequence — например, платёж, который закрывает счёт
  с неверным курсом и потому гасит 1200 на другую сумму). Из открытых книг и provenance
  восстанавливаются книги до внесения ошибок.
"""
from __future__ import annotations

import copy

from . import money
from .config import GENERATOR_VERSION, SCHEMA_VERSION
from .money import fx_id, iso, local_date, rate_str
from .policy_text import build_policy
from .posting import ACC_AR, ACC_CASH, ACC_REVENUE, ACC_UNAPPLIED
from .story import GenerationError
from .util import b32, canonical_json, h, rng


class Renderer:
    def __init__(self, b, clean_rows, rows):
        self.b = b
        self.clean_rows = clean_rows
        self.rows = rows
        self.invoices = b.invoices + b.injected_invoices
        self.payments = b.payments + b.injected_payments
        self.salt = b.cfg["id_salt"]
        self.used = set()
        self.id_map = {}

    # ------------------------------------------------------------ ID
    def pid(self, kind, key, prefix, n=7):
        k = (kind, key)
        if k in self.id_map:
            return self.id_map[k]
        hx = h("id", self.salt, self.b.story_hash, kind, key)
        while prefix + b32(hx, n) in self.used:
            n += 1
        out = prefix + b32(hx, n)
        self.used.add(out)
        self.id_map[k] = out
        return out

    def inv_id(self, k):
        return None if k is None else self.pid("inv", k, "INV-")

    def pay_id(self, k):
        return None if k is None else self.pid("pay", k, "PAY-")

    def doc_id(self, kind, k):
        return self.pid("doc" + kind, k, "DOC-", 8)

    def ent_id(self, k):
        return None if k is None else self.pid("ent", k, "C-", 6)

    def biz_id(self, kind, key):
        return self.inv_id(key) if kind == "inv" else self.pay_id(key)

    # ------------------------------------------------------------ таблицы
    def ledger(self, rows, tag):
        b = self.b
        order = rng("order", b.story_hash, b.cfg["order_seed"], tag)
        tie = [order.random() for _ in rows]
        idx = sorted(range(len(rows)), key=lambda i: (rows[i]["ts"], tie[i]))
        out, keyed = [], []
        for i in idx:
            r_ = rows[i]
            ident = canonical_json([list(r_["entry"]), r_["account"], r_["inv"]])
            rec = {
                "row_id": self.pid("row", ident, "L-", 8),
                "timestamp": iso(r_["ts"]),
                "account": r_["account"],
                "debit_minor": r_["debit"],
                "credit_minor": r_["credit"],
                "currency": b.base,
                "entity_id": self.ent_id(r_["entity"]),
                "invoice_id": self.inv_id(r_["inv"]),
                "payment_id": self.pay_id(r_["pay"]),
            }
            out.append(rec)
            keyed.append((r_, rec))
        return out, keyed

    def inv_rec(self, inv):
        return {
            "doc_id": self.doc_id("inv", inv["key"]),
            "invoice_id": self.inv_id(inv["key"]),
            "entity_id": self.ent_id(inv["entity"]),
            "issued_at": iso(inv["issued"]),
            "due_at": iso(inv["due"]),
            "currency": inv["currency"],
            "total_minor": inv["total"],
            "line_items": copy.deepcopy(inv["lines"]),
        }

    def pay_rec(self, p):
        refs = None
        if p["refs"] is not None:
            refs = [{"invoice_id": self.inv_id(ik), "amount_minor": a} for ik, a in p["refs"]]
        return {
            "doc_id": self.doc_id("pay", p["key"]),
            "payment_id": self.pay_id(p["key"]),
            "timestamp": iso(p["ts"]),
            "currency": p["currency"],
            "amount_minor": p["amount"],
            "invoice_refs": refs,
        }

    def shuffled(self, items, tag):
        items = sorted(items, key=lambda x: x["key"])
        rng("order", self.b.story_hash, self.b.cfg["order_seed"], tag).shuffle(items)
        return items

    def fx_table(self):
        b = self.b
        return [{"fx_id": fx_id(d, c, b.base), "date": d.isoformat(), "source_currency": c,
                 "target_currency": b.base, "rate": rate_str(rate)} for (c, d), rate in sorted(b.fx.items())]

    def manifest(self, episode_id, counts):
        b = self.b
        used = sorted({b.base} | set(b.foreign))
        return {
            "schema_version": SCHEMA_VERSION,
            "generator_version": GENERATOR_VERSION,
            "config_version": b.cfg["config_version"],
            "episode_id": episode_id,
            "company_name": b.company,
            "base_currency": b.base,
            "currency_exponents": {c: money.exponent(c) for c in used},
            "period_start": iso(b.p_start),
            "period_end": iso(b.p_end),
            "period_end_exclusive": True,
            "timezone": b.offset,
            "counts": counts,
            "rounding": {"mode": "ROUND_HALF_EVEN", "unit": "минимальная единица базовой валюты, по каждой строке книги"},
            "money_format": "целые минимальные единицы; основные единицы = значение / 10**currency_exponents[currency]",
            "fx_format": "десятичная строка: 1 единица source_currency = rate единиц target_currency",
            "timestamp_format": "ISO-8601 со смещением UTC",
            "csv_null_token": "null",
            "fictional": "все компании, лица, счета и суммы вымышлены",
        }

    # ------------------------------------------------------------ сборка
    def render(self):
        from .generator import Episode
        b = self.b
        episode_id = "EP-" + b32(h("episode", self.salt, b.story_hash), 10)
        ledger, keyed = self.ledger(self.rows, "")
        invoices = [self.inv_rec(i) for i in self.shuffled(self.invoices, "inv")]
        payments = [self.pay_rec(p) for p in self.shuffled(self.payments, "pay")]
        fx = self.fx_table()
        counts = {"ledger": len(ledger), "journal_entries": len({tuple(r_["entry"]) for r_, _ in keyed}),
                  "invoices": len(invoices), "payments": len(payments), "fx": len(fx),
                  "documents": len(invoices) + len(payments)}
        manifest = self.manifest(episode_id, counts)
        public = {"manifest": manifest, "policy": build_policy(manifest), "ledger": ledger,
                  "invoices": invoices, "payments": payments, "fx": fx}
        cl_ledger, cl_keyed = self.ledger(self.clean_rows, "clean")
        clean_public = {"manifest": dict(manifest, counts={"ledger": len(cl_ledger)}), "policy": public["policy"],
                        "ledger": cl_ledger, "invoices": [self.inv_rec(i) for i in self.shuffled(b.invoices, "inv")],
                        "payments": [self.pay_rec(p) for p in self.shuffled(b.payments, "pay")], "fx": fx}
        truth = {"episode_id": episode_id, "schema_version": SCHEMA_VERSION, "base_currency": b.base,
                 "issues": self.issues(keyed)}
        provenance = self.provenance(episode_id, cl_keyed, keyed)
        meta = {"seed": b.seed, "config": b.cfg, "story_hash": b.story_hash, "attempt": b.attempt}
        return Episode(public, truth, provenance, clean_public, meta)

    # ------------------------------------------------------------ эталон
    @staticmethod
    def _rows(keyed, entry, account=None, inv=None):
        return [rec for r_, rec in keyed if r_["entry"] == entry and (account is None or r_["account"] == account)
                and (inv is None or r_["inv"] == inv)]

    def issues(self, keyed):
        b = self.b
        inv_by = {i["key"]: i for i in self.invoices}
        pay_by = {p["key"]: p for p in self.payments}
        out = []
        for n, iss in enumerate(b.issues):
            t, ev = iss["type"], iss["evidence"]
            if t == "duplicate_invoice":
                _, orig, dup = ev
                ar_dup, ar_orig = self._rows(keyed, ("inv", dup), ACC_AR)[0], self._rows(keyed, ("inv", orig), ACC_AR)[0]
                rev_dup, rev_orig = self._rows(keyed, ("inv", dup), ACC_REVENUE)[0], self._rows(keyed, ("inv", orig), ACC_REVENUE)[0]
                if ar_orig["debit_minor"] != ar_dup["debit_minor"]:
                    raise GenerationError("проводки копий дубля различаются")
                expected = ar_dup["debit_minor"]
                d1, d2 = self.doc_id("inv", orig), self.doc_id("inv", dup)
                sets = [[d1, d2, x["row_id"]] for x in (ar_dup, ar_orig, rev_dup, rev_orig)]
            elif t == "payment_allocation":
                pk = ev[1]
                if ev[0] == "unapplied":
                    row = self._rows(keyed, ("pay", pk), ACC_UNAPPLIED)[0]
                else:
                    row = self._rows(keyed, ("pay", pk), ACC_AR, inv=ev[2])[0]
                expected = row["credit_minor"]
                sets = [[self.doc_id("pay", pk), row["row_id"]]]
            elif t == "fx_rate":
                _, kind, key = ev
                doc = inv_by[key] if kind == "inv" else pay_by[key]
                ts = doc["issued"] if kind == "inv" else doc["ts"]
                amount = doc["total"] if kind == "inv" else doc["amount"]
                pd = local_date(ts, b.offset)
                correct = b.conv(amount, doc["currency"], pd)
                d, fxr = self.doc_id(kind, key), fx_id(pd, doc["currency"], b.base)
                if kind == "inv":
                    main = self._rows(keyed, ("inv", key), ACC_AR)[0]
                    rev = self._rows(keyed, ("inv", key), ACC_REVENUE)[0]
                    sets = [[d, main["row_id"], fxr], [d, rev["row_id"], fxr]]
                else:
                    main = self._rows(keyed, ("pay", key), ACC_CASH)[0]
                    sets = [[d, main["row_id"], fxr]]
                expected = abs(main["debit_minor"] - correct)
            elif t == "period_cutoff":
                _, kind, key = ev
                d = self.doc_id(kind, key)
                if kind == "inv":
                    main = self._rows(keyed, ("inv", key), ACC_AR)[0]
                    rev = self._rows(keyed, ("inv", key), ACC_REVENUE)[0]
                    sets = [[d, main["row_id"]], [d, rev["row_id"]]]
                else:
                    main = self._rows(keyed, ("pay", key), ACC_CASH)[0]
                    sets = [[d, main["row_id"]]]
                expected = main["debit_minor"]
            else:  # pragma: no cover
                raise GenerationError(t)
            if expected <= 0:
                raise GenerationError("неположительная ожидаемая сумма для %s" % t)
            out.append({
                "issue_id": "ISS-" + b32(h("issue", self.salt, b.story_hash, n), 8),
                "type": t,
                "affected_ids": sorted(self.biz_id(k, key) for k, key in iss["affected"]),
                "expected_amount_minor": expected,
                "currency": b.base,
                "tolerance_minor": money.amount_tolerance(expected),
                "evidence_sets": [sorted(s) for s in sets],
                "mutation_id": iss["mutation"],
            })
        out.sort(key=lambda x: x["issue_id"])
        return out

    # ------------------------------------------------------------ provenance
    def provenance(self, episode_id, clean_keyed, keyed):
        b = self.b

        def ref(t):
            kind, key = t
            return {"kind": kind, "id": self.biz_id(kind, key), "doc_id": self.doc_id(kind, key)}

        by_entry_clean, by_entry_after = {}, {}
        for r_, rec in clean_keyed:
            by_entry_clean.setdefault(r_["entry"], []).append(rec)
        for r_, rec in keyed:
            by_entry_after.setdefault(r_["entry"], []).append(rec)
        muts = []
        for m in b.mutations:
            mm = {k: (ref(v) if isinstance(v, tuple) and len(v) == 2 and v[0] in ("inv", "pay") else v)
                  for k, v in m.items()}
            entries = [m[k] for k in ("document", "payment") if k in m]
            mm["postings_before"] = {self.biz_id(*e): by_entry_clean.get(e, []) for e in entries}
            mm["postings_after"] = {self.biz_id(*e): by_entry_after.get(e, []) for e in entries}
            muts.append(mm)
        ledger_diff, docs_added = self.diff(clean_keyed, keyed)
        return {
            "episode_id": episode_id,
            "generator_version": GENERATOR_VERSION,
            "seed": b.seed,
            "config": b.cfg,
            "story_hash": b.story_hash,
            "generation_attempt": b.attempt,
            "steps": [
                "настройка: база=%s, пояс=%s, период=[%s, %s), валюты=%s"
                % (b.base, b.offset, iso(b.p_start), iso(b.p_end), ",".join(b.foreign)),
                "история: %d счетов, %d платежей, %d фоновых операций (без ошибок)"
                % (len(b.invoices), len(b.payments), len(b.background)),
                "чистые проводки: %d строк книги" % len(self.clean_rows),
                "внесение: %d изменений, %d ошибок" % (len(b.mutations), len(b.issues)),
                "рендер: id_salt=%r, order_seed=%r" % (self.salt, b.cfg["order_seed"]),
            ],
            "mutations": muts,
            "ledger_diff": ledger_diff,
            "documents_added": docs_added,
            "reverse_procedure": "книги до внесения = открытая книга минус строки ledger_diff.added (по row_id) "
                                 "плюс строки ledger_diff.removed; счета и платежи минус documents_added (по doc_id)",
            "distractors": self.distractors(),
            "canonical_ids": {pid: "%s:%s" % (kind, key) for (kind, key), pid in sorted(self.id_map.items(), key=lambda kv: kv[1])},
        }

    def diff(self, clean_keyed, keyed):
        b = self.b
        pay_by = {p["key"]: p for p in self.payments}
        direct, touches = {}, {}
        for m in b.mutations:
            for k in ("document", "payment"):
                if k in m:
                    direct.setdefault(m[k], []).append(m["mutation_id"])
            inv_keys = set()
            for k in ("document", "copy_of", "applied_to", "payment"):
                kk = m.get(k)
                if not kk:
                    continue
                if kk[0] == "inv":
                    inv_keys.add(kk[1])
                else:
                    inv_keys.update(ik for ik, _ in (pay_by[kk[1]]["refs"] or []))
            for ik in inv_keys:
                touches.setdefault(ik, []).append(m["mutation_id"])

        def cause(r_):
            if r_["entry"] in direct:
                return sorted(set(direct[r_["entry"]])), "direct"
            invs = {r_["inv"]}
            if r_["entry"][0] == "pay":  # строка 7800 платежа на несколько счетов не привязана к счёту
                invs.update(ik for ik, _ in (pay_by[r_["entry"][1]]["refs"] or []))
            return sorted({mid for ik in invs for mid in touches.get(ik, [])}), "consequence"

        clean = {rec["row_id"]: (r_, rec) for r_, rec in clean_keyed}
        after = {rec["row_id"]: (r_, rec) for r_, rec in keyed}
        removed, added = [], []
        for rid, (r_, rec) in sorted(clean.items()):
            if rid not in after or after[rid][1] != rec:
                c, rel = cause(r_)
                removed.append({"row": rec, "caused_by": c, "relation": rel})
        for rid, (r_, rec) in sorted(after.items()):
            if rid not in clean or clean[rid][1] != rec:
                c, rel = cause(r_)
                added.append({"row": rec, "caused_by": c, "relation": rel})
        story_keys = {("inv", i["key"]) for i in b.invoices} | {("pay", p["key"]) for p in b.payments}
        docs = [{"doc_id": self.doc_id(*m["document"]), "id": self.biz_id(*m["document"]), "caused_by": [m["mutation_id"]]}
                for m in b.mutations if m["document"] not in story_keys]
        return {"removed": removed, "added": added}, docs

    def distractors(self):
        out = {}
        for i in self.b.invoices:
            if i["role"]:
                out.setdefault(i["role"], []).append(self.inv_id(i["key"]))
        for p in self.b.payments:
            if p["role"]:
                out.setdefault(p["role"], []).append(self.pay_id(p["key"]))
        return {k: sorted(v) for k, v in sorted(out.items())}
