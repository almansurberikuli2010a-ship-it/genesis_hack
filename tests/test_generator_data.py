"""Генератор: типы ошибок и их варианты, взаимодействия, чистые компании, отвлекающие случаи,
фоновые проводки, проверки данных, безвредные преобразования, экспорт с кириллицей."""
import json
import random
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from auditgym.audit_rules import book_invariants, rule_audit
from auditgym.config import DISTRACTORS, ERROR_TYPES, GENERATOR_VERSION, INTERACTIONS, MAX_DOCUMENTS, ConfigError, normalize_config, story_part
from auditgym.datachecks import check_episode, check_records
from auditgym.export import load_episode_files, load_public_dir, write_episode
from auditgym.generator import generate
from auditgym.money import convert_minor, in_period, local_date, parse_ts, tz_from_offset
from auditgym.provenance_tools import reconstruct_pre_injection, same_books, unattributed_changes
from auditgym.story import StoryBuilder
from auditgym.util import SLOW_KDF, canonical_json, slow_h


class ErrorTypes(unittest.TestCase):
    def test_each_type_independently_configurable(self):
        for t in ERROR_TYPES:
            for seed in (1, 2, 3):
                ep = generate(seed, {"errors": {t: 2}})
                self.assertEqual([i["type"] for i in ep.truth["issues"]], [t, t])
                self.assertEqual(check_episode(ep), [])

    def test_all_variants_occur(self):
        seen = set()
        for seed in range(60):
            ep = generate(seed, {"errors": {"fx_rate": 3, "period_cutoff": 3, "payment_allocation": 2}})
            seen |= {(m["issue_type"], m.get("variant")) for m in ep.provenance["mutations"]}
        for v in (("fx_rate", "wrong_date"), ("fx_rate", "rounded_rate"), ("fx_rate", "inverted_rate"),
                  ("period_cutoff", "next_period"), ("period_cutoff", "prior_period"),
                  ("payment_allocation", "missing"), ("payment_allocation", "partial")):
            self.assertIn(v, seen)

    def test_clean_company(self):
        for seed in range(8):
            ep = generate(seed, {})
            self.assertEqual(ep.truth["issues"], [])
            self.assertEqual(rule_audit(ep.public), [])
            self.assertEqual(book_invariants(ep.public), [])

    def test_interaction_partial_fx_boundary(self):
        for seed in range(12):
            ep = generate(seed, {"preset": "interacting_partial_fx_boundary"})
            self.assertEqual(sorted(i["type"] for i in ep.truth["issues"]), ["fx_rate", "payment_allocation"])
            a, b = (set(i["affected_ids"]) for i in ep.truth["issues"])
            pid = next(iter(a & b))
            p = next(x for x in ep.public["payments"] if x["payment_id"] == pid)
            m = ep.public["manifest"]
            self.assertNotEqual(p["currency"], m["base_currency"])
            inv = next(x for x in ep.public["invoices"] if x["invoice_id"] == p["invoice_refs"][0]["invoice_id"])
            self.assertLess(p["amount_minor"], inv["total_minor"])
            last = local_date(m["period_end"], m["timezone"]).toordinal() - 1
            self.assertEqual(local_date(p["timestamp"], m["timezone"]).toordinal(), last)
            self.assertEqual(check_episode(ep), [])

    def test_interaction_masked_duplicate(self):
        for seed in range(12):
            ep = generate(seed, {"preset": "interacting_masked_duplicate"})
            by = {i["type"]: i for i in ep.truth["issues"]}
            self.assertEqual(set(by), {"duplicate_invoice", "payment_allocation"})
            self.assertTrue(set(by["payment_allocation"]["affected_ids"]) & set(by["duplicate_invoice"]["affected_ids"]))
            self.assertEqual(check_episode(ep), [])

    def test_distractors_present_and_clean(self):
        ep = generate(3, {"preset": "clean_hard"})
        d = ep.provenance["distractors"]
        for k in DISTRACTORS:
            self.assertIn(k, d, k)
        pays = ep.public["payments"]
        self.assertTrue(any(p["invoice_refs"] is None for p in pays))
        self.assertTrue(any(p["amount_minor"] < 0 for p in pays))
        self.assertTrue(any(p["invoice_refs"] and len(p["invoice_refs"]) > 1 for p in pays))
        m = ep.public["manifest"]
        out = [x for x in ep.public["invoices"] + pays
               if not in_period(x.get("issued_at") or x["timestamp"], m["period_start"], m["period_end"])]
        self.assertTrue(out)  # документы вне периода есть и правильно не проведены
        self.assertEqual(rule_audit(ep.public), [])
        self.assertEqual(check_episode(ep), [])

    def test_background_operating_entries(self):
        ep = generate(4, {})
        ops = [r for r in ep.public["ledger"] if r["invoice_id"] is None and r["payment_id"] is None]
        self.assertTrue(ops)
        self.assertTrue(all(r["account"][:1] == "6" or r["account"].startswith("1000") for r in ops))
        self.assertEqual(sum(r["debit_minor"] - r["credit_minor"] for r in ops), 0)

    def test_sizes_within_suggested_ranges(self):
        for seed in range(6):
            for preset in ("clean", "mixed", "heavy"):
                c = generate(seed, {"preset": preset}).public["manifest"]["counts"]
                self.assertTrue(200 <= c["ledger"] <= 2000, c)
                self.assertTrue(200 <= c["journal_entries"] <= 2000, c)
                self.assertTrue(20 <= c["documents"] <= 200, c)
        self.assertEqual(len(generate(9, {"preset": "heavy"}).truth["issues"]), 12)

    def test_formats(self):
        ep = generate(4, {"errors": {"fx_rate": 1}})
        m = ep.public["manifest"]
        for r in ep.public["ledger"]:
            self.assertIsInstance(r["debit_minor"], int)
            self.assertRegex(r["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
            self.assertTrue(in_period(r["timestamp"], m["period_start"], m["period_end"]))
        for f in ep.public["fx"]:
            self.assertIsInstance(f["rate"], str)
            Decimal(f["rate"])
        for i in ep.public["invoices"]:
            self.assertRegex(i["issued_at"], r"[+-]\d\d:\d\d$")
            self.assertEqual(sum(l["amount_minor"] for l in i["line_items"]), i["total_minor"])

    def test_fx_issue_amount_recomputable(self):
        ep = generate(8, {"errors": {"fx_rate": 3}})
        m = ep.public["manifest"]
        fx = {f["fx_id"]: f for f in ep.public["fx"]}
        rows = {r["row_id"]: r for r in ep.public["ledger"]}
        docs = {d["doc_id"]: d for d in ep.public["invoices"] + ep.public["payments"]}
        for t in ep.truth["issues"]:
            es = t["evidence_sets"][0]
            f = fx[[e for e in es if e.startswith("FX-")][0]]
            row = rows[[e for e in es if e.startswith("L-")][0]]
            doc = docs[[e for e in es if e.startswith("DOC-")][0]]
            amount = doc.get("total_minor", doc.get("amount_minor"))
            correct = convert_minor(amount, doc["currency"], m["base_currency"], Decimal(f["rate"]))
            self.assertEqual(abs(row["debit_minor"] + row["credit_minor"] - correct), t["expected_amount_minor"])


class Robustness(unittest.TestCase):
    def test_random_extreme_configs(self):
        rnd = random.Random(99)
        for i in range(50):
            cfg = {"errors": {t: rnd.randint(0, 4) for t in ERROR_TYPES}, "interactions": {t: rnd.randint(0, 2) for t in INTERACTIONS},
                   "n_invoices": sorted([rnd.randint(20, 120), rnd.randint(20, 120)]),
                   "n_customers": sorted([rnd.randint(3, 40), rnd.randint(3, 40)]),
                   "background_entries": sorted([rnd.randint(0, 200), rnd.randint(0, 200)]),
                   "foreign_share": rnd.choice([0, 0.1, 1]), "distractors": {k: rnd.randint(0, 5) for k in DISTRACTORS}}
            while sum(cfg["errors"].values()) + 2 * sum(cfg["interactions"].values()) > 12:
                k = rnd.choice(ERROR_TYPES)
                cfg["errors"][k] = max(0, cfg["errors"][k] - 1)
                if not sum(cfg["errors"].values()):
                    cfg["interactions"] = {t: 0 for t in INTERACTIONS}
            ep = generate(70000 + i, cfg)
            self.assertLessEqual(ep.public["manifest"]["counts"]["documents"], MAX_DOCUMENTS)
            self.assertEqual(check_episode(ep), [], cfg)

    def test_all_fx_errors_without_foreign_customers(self):
        ep = generate(3, {"errors": {"fx_rate": 12}, "foreign_share": 0})
        self.assertEqual(len(ep.truth["issues"]), 12)
        self.assertEqual(check_episode(ep), [])

    def test_too_many_issues_is_config_error(self):
        with self.assertRaises(ConfigError):
            generate(1, {"errors": {"fx_rate": 7}, "interactions": {"masked_duplicate": 3}})


class Provenance(unittest.TestCase):
    def test_pre_injection_rebuilt_from_provenance_alone(self):
        for seed in range(10):
            ep = generate(seed, {"preset": "heavy"})
            rebuilt = reconstruct_pre_injection(ep.public, ep.provenance)
            self.assertEqual(same_books(rebuilt, ep.clean_public), [])
            self.assertEqual(book_invariants(rebuilt), [])
            self.assertEqual(unattributed_changes(ep.provenance), [])
            self.assertNotEqual(same_books(ep.public, rebuilt), [])

    def test_consequences_attributed(self):
        rels = set()
        for seed in range(30):
            rels |= {x["relation"] for x in generate(seed, {"errors": {"fx_rate": 4, "payment_allocation": 2}}).provenance["ledger_diff"]["added"]}
        self.assertEqual(rels, {"direct", "consequence"})


class Transformations(unittest.TestCase):
    def test_salt_and_order_change_ids_not_economics(self):
        base = {"preset": "four_types", "interactions": {"masked_duplicate": 1}}
        a, b = generate(21, base), generate(21, dict(base, id_salt="другая", order_seed=99))
        self.assertNotEqual(a.episode_id, b.episode_id)
        self.assertFalse({i["invoice_id"] for i in a.public["invoices"]} & {i["invoice_id"] for i in b.public["invoices"]})
        self.assertEqual(sorted((t["type"], t["expected_amount_minor"]) for t in a.truth["issues"]),
                         sorted((t["type"], t["expected_amount_minor"]) for t in b.truth["issues"]))
        self.assertEqual(check_episode(b), [])

    def test_determinism_and_preset_equivalence(self):
        self.assertEqual(generate(33, {"errors": {"fx_rate": 2}}).digest(), generate(33, {"errors": {"fx_rate": 2}}).digest())
        a = generate(5, {"preset": "four_types", "errors": {"fx_rate": 2}})
        b = generate(5, {"errors": {"duplicate_invoice": 1, "payment_allocation": 1, "fx_rate": 2, "period_cutoff": 1}})
        self.assertEqual(a.digest(), b.digest())


def _boundary_like(ep):
    """Платежи, похожие на сценарий partial_fx_boundary по открытым документам (без книги):
    частичный валютный платёж в последний локальный день периода, у смены даты по UTC."""
    m = ep.public["manifest"]
    tot = {i["invoice_id"]: i["total_minor"] for i in ep.public["invoices"]}
    last = local_date(m["period_end"], m["timezone"]).toordinal() - 1
    out = set()
    for p in ep.public["payments"]:
        refs = p["invoice_refs"]
        if p["currency"] == m["base_currency"] or not refs or len(refs) != 1:
            continue
        ts = parse_ts(p["timestamp"])
        if 0 < p["amount_minor"] < tot[refs[0]["invoice_id"]] and local_date(ts, m["timezone"]).toordinal() == last:
            out.add(p["payment_id"])
    return out


class SurfaceFeatures(unittest.TestCase):
    """Документы ошибок не выдаются поверхностными признаками: время, секунды и смещение платежа
    сценария partial_fx_boundary — из общего распределения, у него есть корректный двойник."""

    @classmethod
    def setUpClass(cls):
        cls.eps = [generate(seed, {"preset": "interacting_partial_fx_boundary"}) for seed in range(24)]

    def test_boundary_payments_not_marked_by_seconds_or_offset(self):
        scen = []
        for ep in self.eps:
            pids = {m["document"]["id"] for m in ep.provenance["mutations"] if m.get("scenario") == "partial_fx_boundary"}
            scen += [p["timestamp"] for p in ep.public["payments"] if p["payment_id"] in pids]
        self.assertEqual(len(scen), len(self.eps))
        self.assertLess(sum(parse_ts(t).second == 0 for t in scen), len(scen) // 4)          # секунды не всегда :00
        self.assertLess(sum(t.endswith("+00:00") for t in scen), len(scen) * 3 // 4)       # смещение не всегда UTC

    def test_correct_twin_makes_surface_rule_imprecise(self):
        tp = fp = 0
        for ep in self.eps:
            twins = set(ep.provenance["distractors"].get("boundary_fx_partial", []))
            self.assertTrue(twins)
            pids = {m["document"]["id"] for m in ep.provenance["mutations"] if m.get("scenario") == "partial_fx_boundary"}
            like = _boundary_like(ep)
            self.assertLessEqual(pids, like)                                    # правило находит сценарий...
            self.assertTrue({x for x in twins if x.startswith("PAY-")} <= like)  # ...и всех двойников
            tp, fp = tp + len(like & pids), fp + len(like - pids)
            flagged = {a for f in rule_audit(ep.public) for a in f["affected_ids"]}
            self.assertFalse(flagged & twins)                                   # двойник корректен
            self.assertEqual(check_episode(ep), [])
        self.assertLessEqual(tp / (tp + fp), 0.6)

    def test_ordinary_payments_reach_period_end_and_night(self):
        late = night = 0
        for seed in range(12):
            ep = generate(seed, {"preset": "clean"})
            m = ep.public["manifest"]
            end, tz = parse_ts(m["period_end"]), tz_from_offset(m["timezone"])
            for p in ep.public["payments"]:
                ts = parse_ts(p["timestamp"])
                late += end.timestamp() - 6 * 3600 <= ts.timestamp() < end.timestamp()
                night += ts.astimezone(tz).hour < 7
        self.assertGreater(late, 0)
        self.assertGreater(night, 0)


class SeedRecovery(unittest.TestCase):
    def test_story_key_is_slow_kdf_of_economics_only(self):
        cfg = normalize_config({"preset": "mixed"})
        b = StoryBuilder(7, cfg)
        self.assertEqual(b.story_key, slow_h("story-key", GENERATOR_VERSION, 7, canonical_json(story_part(cfg))))
        salted = normalize_config({"preset": "mixed", "id_salt": "x", "order_seed": 5})
        self.assertEqual(StoryBuilder(7, salted).story_key, b.story_key)  # id_salt и order_seed экономику не меняют
        self.assertNotEqual(StoryBuilder(8, cfg).story_key, b.story_key)
        self.assertGreaterEqual(128 * SLOW_KDF["r"] * SLOW_KDF["n"], 8 * 2 ** 20)  # память-ёмкая: от 8 МиБ на вызов


class Export(unittest.TestCase):
    def test_round_trip_utf8_and_separation(self):
        with tempfile.TemporaryDirectory() as d:
            ep = generate(4, {"errors": {"period_cutoff": 1, "payment_allocation": 1}})
            write_episode(ep, Path(d))
            pdir, gdir = Path(d) / "public" / ep.episode_id, Path(d) / "grader" / ep.episode_id
            self.assertEqual(sorted(p.name for p in pdir.iterdir()),
                             ["fx.csv", "invoices.jsonl", "ledger.csv", "manifest.json", "payments.jsonl", "policy.json"])
            self.assertEqual(sorted(p.name for p in gdir.iterdir()), ["pre_injection", "provenance.json", "truth.json"])
            back = load_public_dir(pdir)
            for k in ("ledger", "invoices", "payments", "fx", "manifest", "policy"):
                self.assertEqual(back[k], ep.public[k], k)
            text = (pdir / "ledger.csv").read_bytes().decode("utf-8")
            self.assertEqual(text.splitlines()[0],
                             "row_id,timestamp,account,debit_minor,credit_minor,currency,entity_id,invoice_id,payment_id")
            self.assertIn("Дебиторская задолженность", text)
            self.assertIn(",null", text)
            self.assertEqual(check_records(*load_episode_files(Path(d), ep.episode_id)), [])


if __name__ == "__main__":
    unittest.main()
