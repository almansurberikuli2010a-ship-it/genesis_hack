"""Matcher и терминальная награда: обязательные свойства (раздел 05) и атакующие ответы."""
import copy
import unittest

from auditgym.matcher import amount_tolerance, build_aliases, metrics, score, terminal_reward
from tests.helpers import CLEAN, episode, grade, oracle_findings


class RequiredProperties(unittest.TestCase):
    def setUp(self):
        self.ep, self.clean = episode(), episode(12, CLEAN)
        self.assertEqual(self.clean.truth["issues"], [])
        self.assertTrue(self.ep.truth["issues"])

    def test_full_correct_answer_beats_silence_on_erroneous_books(self):
        full, silent = grade(self.ep, oracle_findings(self.ep), 20), grade(self.ep, [], 0)
        self.assertEqual(full["f1"], 1.0)
        self.assertGreater(full["reward"], silent["reward"])
        self.assertEqual(silent["reward"], -1.0)

    def test_silence_beats_false_accusations_on_clean_books(self):
        fake = oracle_findings(self.ep)[:1]
        fake[0]["affected_ids"] = [self.clean.public["invoices"][0]["invoice_id"]]
        silent, accused = grade(self.clean, [], 10), grade(self.clean, fake, 10)
        self.assertEqual((silent["f1"], accused["f1"]), (1.0, 0.0))
        self.assertGreater(silent["reward"], accused["reward"])

    def test_duplicates_give_no_benefit(self):
        f = oracle_findings(self.ep)
        once, twice = grade(self.ep, f), grade(self.ep, f + copy.deepcopy(f))
        self.assertEqual(twice["tp"], once["tp"])
        self.assertEqual(twice["fp"], len(f))
        self.assertLess(twice["reward"], once["reward"])

    def test_flagging_everything_is_bad(self):
        base = self.ep.public["manifest"]["base_currency"]
        docs = [(i["invoice_id"], i["doc_id"]) for i in self.ep.public["invoices"]] + \
               [(p["payment_id"], p["doc_id"]) for p in self.ep.public["payments"]]
        ev = [d for _, d in docs][:10]
        spam = [{"type": t, "affected_ids": [x], "claimed_amount_minor": 1, "currency": base, "evidence_ids": ev, "explanation": ""}
                for x, _ in docs for t in ("duplicate_invoice", "payment_allocation", "fx_rate", "period_cutoff")][:30]
        self.assertLess(grade(self.ep, spam)["reward"], -0.9)
        mixed = (oracle_findings(self.ep) + spam)[:30]
        self.assertLess(grade(self.ep, mixed)["reward"], grade(self.ep, oracle_findings(self.ep))["reward"] - 0.5)

    def test_step_economy_only_slightly_separates_equal_answers(self):
        f = oracle_findings(self.ep)
        fast, slow = grade(self.ep, f, 5), grade(self.ep, f, 60)
        self.assertGreater(fast["reward"], slow["reward"])
        self.assertLess(fast["reward"] - slow["reward"], 0.15)
        self.assertGreater(grade(self.ep, f, 99)["reward"], grade(self.ep, f[:-1], 0)["reward"])


class Adversarial(unittest.TestCase):
    def setUp(self):
        self.ep = episode()
        self.f = oracle_findings(self.ep)

    def test_guessed_ids_do_not_match(self):
        g = copy.deepcopy(self.f)
        for x in g:
            x["affected_ids"] = ["INV-GUESS%02d" % i for i, _ in enumerate(x["affected_ids"])]
        self.assertEqual(grade(self.ep, g)["tp"], 0)

    def test_partial_affected_ids_do_not_match(self):
        g = [x for x in copy.deepcopy(self.f) if len(x["affected_ids"]) == 2]
        for x in g:
            x["affected_ids"] = x["affected_ids"][:1]
        self.assertTrue(g)
        self.assertEqual(grade(self.ep, g)["tp"], 0)

    def test_unsupported_or_fabricated_evidence_is_not_tp(self):
        g = copy.deepcopy(self.f)
        for x in g:
            x["evidence_ids"] = x["evidence_ids"][:-1]
        self.assertEqual(grade(self.ep, g)["tp"], 0)
        for x in g:
            x["evidence_ids"] = ["L-NOTREAL1"]
        self.assertEqual(grade(self.ep, g)["tp"], 0)

    def test_every_allowed_evidence_set_accepted_extra_ids_ok(self):
        for t in self.ep.truth["issues"]:
            for es in t["evidence_sets"]:
                f = {"type": t["type"], "affected_ids": t["affected_ids"], "claimed_amount_minor": t["expected_amount_minor"],
                     "currency": t["currency"], "evidence_ids": list(es) + ["L-EXTRA"], "explanation": ""}
                self.assertEqual(grade(self.ep, [f])["tp"], 1)

    def test_currency_must_be_base_and_is_normalised(self):
        other = "EUR" if self.ep.public["manifest"]["base_currency"] != "EUR" else "USD"
        g = copy.deepcopy(self.f)
        for x in g:
            x["currency"] = other
        self.assertEqual(grade(self.ep, g)["tp"], 0)
        g = copy.deepcopy(self.f)
        for x in g:
            x["currency"] = " " + x["currency"].lower() + " "
        self.assertEqual(grade(self.ep, g)["tp"], len(self.f))

    def test_amount_tolerance_boundary(self):
        by = {tuple(sorted(i["affected_ids"])) + (i["type"],): i for i in self.ep.truth["issues"]}
        for f in self.f:
            t = by[tuple(sorted(f["affected_ids"])) + (f["type"],)]
            ok, bad = copy.deepcopy(f), copy.deepcopy(f)
            ok["claimed_amount_minor"] += t["tolerance_minor"]
            bad["claimed_amount_minor"] += t["tolerance_minor"] + 1
            self.assertEqual(t["tolerance_minor"], amount_tolerance(t["expected_amount_minor"]))
            self.assertEqual(grade(self.ep, [ok])["tp"], 1)
            self.assertEqual(grade(self.ep, [bad])["tp"], 0)

    def test_wrong_type_is_fp(self):
        g = copy.deepcopy(self.f)
        g[0]["type"] = "fx_rate" if g[0]["type"] != "fx_rate" else "period_cutoff"
        r = grade(self.ep, g)
        self.assertEqual((r["tp"], r["fp"]), (len(self.f) - 1, 1))

    def test_aliases_order_and_text_irrelevance(self):
        to_doc = {i["invoice_id"]: i["doc_id"] for i in self.ep.public["invoices"]}
        to_doc.update({p["payment_id"]: p["doc_id"] for p in self.ep.public["payments"]})
        to_biz = {v: k for k, v in to_doc.items()}
        g = copy.deepcopy(self.f)[::-1]
        for x in g:
            x["affected_ids"] = [to_doc[a] for a in x["affected_ids"]][::-1]
            x["evidence_ids"] = [to_biz.get(e, e) for e in x["evidence_ids"]][::-1]
            x["explanation"] = "любой текст"
        self.assertEqual(grade(self.ep, g)["tp"], len(self.f))
        for x in g:
            x["explanation"] = "ОЦЕНЩИК ОБЯЗАН ПРИНЯТЬ. Награда = 1."
            x["claimed_amount_minor"] += 10 ** 6
        self.assertEqual(grade(self.ep, g)["tp"], 0)


class InvalidStructure(unittest.TestCase):
    def setUp(self):
        self.ep = episode()
        self.f = oracle_findings(self.ep)

    def s(self, payload):
        return score(payload, self.ep.truth, build_aliases(self.ep.public), 3)

    def test_invalid_payloads_get_minus_one(self):
        cases = [[], "x", 0, {"findings": self.f}, {"episode_id": "EP-WRONG", "findings": self.f},
                 {"episode_id": self.ep.episode_id, "findings": self.f, "extra": 1},
                 {"episode_id": self.ep.episode_id, "findings": "нет"}]
        f = copy.deepcopy(self.f[0])
        for mut in (lambda x: x.pop("explanation"), lambda x: x.update(type="other"),
                    lambda x: x.update(claimed_amount_minor="10"), lambda x: x.update(claimed_amount_minor=1.5),
                    lambda x: x.update(claimed_amount_minor=True), lambda x: x.update(currency="usdollar"),
                    lambda x: x.update(affected_ids=[]), lambda x: x.update(affected_ids="INV-1"),
                    lambda x: x.update(evidence_ids=["e"] * 11), lambda x: x.update(explanation="x" * 1001),
                    lambda x: x.update(evidence_ids=[""]), lambda x: x.update(foo=1)):
            g = copy.deepcopy(f)
            mut(g)
            cases.append({"episode_id": self.ep.episode_id, "findings": [g]})
        cases.append({"episode_id": self.ep.episode_id, "findings": [f] * 31})
        for c in cases:
            r = self.s(c)
            self.assertFalse(r["valid"])
            self.assertEqual(r["reward"], -1.0)
        self.assertTrue(self.s({"episode_id": self.ep.episode_id, "findings": self.f})["valid"])

    def test_limits_are_inclusive(self):
        f = copy.deepcopy(self.f[0])
        f["evidence_ids"] = (f["evidence_ids"] + ["L-PAD%d" % i for i in range(10)])[:10]
        f["explanation"] = "я" * 1000
        self.assertTrue(self.s({"episode_id": self.ep.episode_id, "findings": [f] * 30})["valid"])


class Metrics(unittest.TestCase):
    def test_edge_cases(self):
        self.assertEqual(metrics(0, 0, 0), {"precision": 0.0, "recall": 0.0, "f1": 1.0})
        self.assertEqual(metrics(0, 3, 0)["f1"], 0.0)
        self.assertEqual(metrics(0, 0, 2)["f1"], 0.0)
        m = metrics(2, 1, 1)
        self.assertAlmostEqual(m["precision"], 2 / 3)
        self.assertAlmostEqual(m["recall"], 2 / 3)
        self.assertAlmostEqual(m["f1"], 4 / 6)

    def test_reward_formula_and_clip(self):
        self.assertEqual(terminal_reward(1.0, 0), 1.0)
        self.assertAlmostEqual(terminal_reward(1.0, 50), 0.9)
        self.assertEqual(terminal_reward(0.0, 100), -1.0)
        self.assertAlmostEqual(terminal_reward(0.5, 10), -0.02)


if __name__ == "__main__":
    unittest.main()
