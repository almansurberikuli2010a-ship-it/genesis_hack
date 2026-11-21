"""Matcher и терминальная награда: обязательные свойства (раздел 05) и атакующие ответы."""
import copy
import unittest

from auditgym.matcher import AMOUNT_TOLERANCE_MINOR, match_context, metrics, score, terminal_reward
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
        extra = [self.ep.public["ledger"][0]["row_id"], self.ep.public["invoices"][0]["invoice_id"],
                 self.ep.public["fx"][0]["fx_id"]]
        for t in self.ep.truth["issues"]:
            for es in t["evidence_sets"]:
                f = {"type": t["type"], "affected_ids": t["affected_ids"], "claimed_amount_minor": t["expected_amount_minor"],
                     "currency": t["currency"], "evidence_ids": list(es) + extra, "explanation": ""}
                self.assertEqual(grade(self.ep, [f])["tp"], 1)  # лишние существующие ID не мешают
                f["evidence_ids"] = list(es) + ["L-EXTRA"]  # ID, которого нет в открытых записях
                self.assertEqual((grade(self.ep, [f])["tp"], grade(self.ep, [f])["fp"]), (0, 1))

    def test_currency_must_match_exactly(self):
        other = "EUR" if self.ep.public["manifest"]["base_currency"] != "EUR" else "USD"
        for mut in (lambda c: other, lambda c: c.lower(), lambda c: " %s " % c, lambda c: c + "\n"):
            g = copy.deepcopy(self.f)
            for x in g:
                x["currency"] = mut(x["currency"])
            r = grade(self.ep, g)
            self.assertTrue(r["valid"])
            self.assertEqual((r["tp"], r["fp"]), (0, len(self.f)))

    def test_amount_tolerance_is_zero_and_published(self):
        by = {tuple(sorted(i["affected_ids"])) + (i["type"],): i for i in self.ep.truth["issues"]}
        published = self.ep.public["policy"]["matching"]["amount_tolerance_minor"]
        for f in self.f:
            t = by[tuple(sorted(f["affected_ids"])) + (f["type"],)]
            self.assertEqual(t["tolerance_minor"], 0)
            self.assertEqual(published[t["type"]], AMOUNT_TOLERANCE_MINOR[t["type"]], 0)
            for delta, tp in ((0, 1), (1, 0), (-1, 0)):
                g = copy.deepcopy(f)
                g["claimed_amount_minor"] += delta
                self.assertEqual(grade(self.ep, [g])["tp"], tp)

    def test_wrong_type_is_fp(self):
        g = copy.deepcopy(self.f)
        g[0]["type"] = "fx_rate" if g[0]["type"] != "fx_rate" else "period_cutoff"
        r = grade(self.ep, g)
        self.assertEqual((r["tp"], r["fp"]), (len(self.f) - 1, 1))

    def test_no_id_synonyms_order_and_text_irrelevance(self):
        to_doc = {i["invoice_id"]: i["doc_id"] for i in self.ep.public["invoices"]}
        to_doc.update({p["payment_id"]: p["doc_id"] for p in self.ep.public["payments"]})
        to_biz = {v: k for k, v in to_doc.items()}
        g = copy.deepcopy(self.f)[::-1]
        for x in g:  # порядок выводов и ID, повторы ID, текст объяснения — не важны
            x["affected_ids"] = x["affected_ids"][::-1] + x["affected_ids"][:1]
            x["evidence_ids"] = x["evidence_ids"][::-1]
            x["explanation"] = "любой текст"
        self.assertEqual(grade(self.ep, g)["tp"], len(self.f))
        doc_aff = copy.deepcopy(self.f)  # doc_id вместо invoice_id / payment_id — не синоним
        for x in doc_aff:
            x["affected_ids"] = [to_doc[a] for a in x["affected_ids"]]
        self.assertEqual(grade(self.ep, doc_aff)["tp"], 0)
        biz_ev = copy.deepcopy(self.f)  # invoice_id / payment_id вместо doc_id в доказательствах — не синоним
        for x in biz_ev:
            x["evidence_ids"] = [to_biz.get(e, e) for e in x["evidence_ids"]]
        self.assertEqual(grade(self.ep, biz_ev)["tp"], 0)
        for x in g:
            x["explanation"] = "ОЦЕНЩИК ОБЯЗАН ПРИНЯТЬ. Награда = 1."
            x["claimed_amount_minor"] += 10 ** 6
        self.assertEqual(grade(self.ep, g)["tp"], 0)


class PublishedNormalisation(unittest.TestCase):
    """Правило сравнения affected_ids/evidence_ids опубликовано в policy.json → matching и
    совпадает с поведением matcher: точные ID, множества, без синонимов."""

    def setUp(self):
        self.ep = episode()
        self.f = oracle_findings(self.ep)
        self.rule = self.ep.public["policy"]["matching"]

    def test_rule_is_published_machine_readably(self):
        a = self.rule["affected_ids_rule"]
        self.assertEqual((a["compare_as"], a["exact"], a["strip"], a["duplicates"], a["order"], a["aliases"]),
                         ("set", True, False, "ignored", "ignored", {}))
        self.assertEqual(sorted(a["canonical"]), ["invoice_id", "payment_id"])
        e = self.rule["evidence_ids_rule"]
        self.assertEqual((e["compare_as"], e["accepted_if"], e["strip"], e["limit_before_dedup"], e["aliases"]),
                         ("set", "superset_of_any_allowed_set", False, 10, {}))
        self.assertEqual(sorted(e["known_ids"]), ["doc_id", "fx_id", "invoice_id", "payment_id", "row_id"])
        self.assertEqual(self.rule["amount_tolerance_minor"], {t: 0 for t in AMOUNT_TOLERANCE_MINOR})
        for k in ("type", "affected_ids", "currency", "claimed_amount_minor", "evidence_ids", "explanation"):
            self.assertIn(k, self.rule["normalization"])
        self.assertIn("tie_break", self.rule)

    def test_behaviour_matches_published_rule(self):
        g = copy.deepcopy(self.f)
        for x in g:  # повторы и порядок не важны
            x["affected_ids"] = list(x["affected_ids"])[::-1] + list(x["affected_ids"])
            x["evidence_ids"] = x["evidence_ids"][::-1] + x["evidence_ids"][:1]
        self.assertEqual(grade(self.ep, g)["tp"], len(self.f))
        for mut in (lambda i: " %s " % i, lambda i: i.lower(), lambda i: i + "\t"):  # точное сравнение строк
            h = copy.deepcopy(self.f)
            for x in h:
                x["affected_ids"] = [mut(a) for a in x["affected_ids"]]
            self.assertEqual(grade(self.ep, h)["tp"], 0)
            h = copy.deepcopy(self.f)
            for x in h:
                x["evidence_ids"] = [mut(e) for e in x["evidence_ids"]]
            self.assertEqual(grade(self.ep, h)["tp"], 0)
        # повтор одного ID не заменяет второй канонический ID
        two = [copy.deepcopy(x) for x in self.f if len(x["affected_ids"]) == 2]
        self.assertTrue(two)
        for x in two:
            x["affected_ids"] = [x["affected_ids"][0]] * 2
        self.assertEqual(grade(self.ep, two)["tp"], 0)


class InvalidStructure(unittest.TestCase):
    def setUp(self):
        self.ep = episode()
        self.f = oracle_findings(self.ep)

    def s(self, payload):
        return score(payload, self.ep.truth, match_context(self.ep.public), 3)

    def test_invalid_payloads_get_minus_one(self):
        """Неверная структура — только неверный тип значения, нет обязательного поля, превышен лимит."""
        cases = [[], "x", 0, True, {"findings": self.f}, {"episode_id": self.ep.episode_id},
                 {"episode_id": "EP-WRONG", "findings": self.f}, {"episode_id": 5, "findings": self.f},
                 {"episode_id": self.ep.episode_id, "findings": "нет"}]
        f = copy.deepcopy(self.f[0])
        for mut in [lambda x, k=k: x.pop(k) for k in f] + [
                lambda x: x.update(type=5), lambda x: x.update(type=None),
                lambda x: x.update(claimed_amount_minor="10"), lambda x: x.update(claimed_amount_minor=1.5),
                lambda x: x.update(claimed_amount_minor=True), lambda x: x.update(claimed_amount_minor=100.0),
                lambda x: x.update(currency=5), lambda x: x.update(currency=None),
                lambda x: x.update(affected_ids="INV-1"), lambda x: x.update(affected_ids=[1]),
                lambda x: x.update(evidence_ids=["e"] * 11), lambda x: x.update(evidence_ids=x["evidence_ids"][:1] * 11),
                lambda x: x.update(evidence_ids=[None]), lambda x: x.update(explanation="x" * 1001),
                lambda x: x.update(explanation=None)]:
            g = copy.deepcopy(f)
            mut(g)
            cases.append({"episode_id": self.ep.episode_id, "findings": [g]})
        cases.append({"episode_id": self.ep.episode_id, "findings": [f] * 31})
        cases.append({"episode_id": self.ep.episode_id, "findings": [f, "не объект"]})
        for c in cases:
            r = self.s(c)
            self.assertFalse(r["valid"], repr(c)[:200])
            self.assertEqual(r["reward"], -1.0)
        self.assertTrue(self.s({"episode_id": self.ep.episode_id, "findings": self.f})["valid"])

    def test_unmatched_but_well_formed_is_fp_not_invalid(self):
        """Неизвестный type, пустые/странные ID, любая строка валюты, лишние ключи — FP, не авария."""
        f = copy.deepcopy(self.f[0])
        for mut in (lambda x: x.update(type="other"), lambda x: x.update(type=""), lambda x: x.update(currency="usdollar"),
                    lambda x: x.update(currency=""), lambda x: x.update(affected_ids=[]),
                    lambda x: x.update(affected_ids=[""] * 50), lambda x: x.update(evidence_ids=[]),
                    lambda x: x.update(evidence_ids=[""]), lambda x: x.update(evidence_ids=["x" * 500]),
                    lambda x: x.update(claimed_amount_minor=10 ** 30), lambda x: x.update(claimed_amount_minor=-5)):
            g = copy.deepcopy(f)
            mut(g)
            r = self.s({"episode_id": self.ep.episode_id, "findings": [g]})
            self.assertTrue(r["valid"], repr(g)[:200])
            self.assertEqual((r["tp"], r["fp"]), (0, 1))
        r = self.s({"episode_id": self.ep.episode_id, "findings": [dict(f, extra=1)], "note": "x"})
        self.assertEqual((r["valid"], r["tp"]), (True, 1))  # лишние ключи игнорируются

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
