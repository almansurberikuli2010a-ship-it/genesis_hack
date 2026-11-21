"""Гарантии генератора: любой принятый конфиг генерируется и выполняется точно, невозможный
отвергается ConfigError; нет строк книги с нулевой суммой; правило fx_id объявлено в manifest.
Большой фаззинг (тысячи конфигов) — отдельным прогоном; здесь небольшой и быстрый."""
import copy
import csv
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from auditgym import config as config_module
from auditgym.audit_rules import book_invariants
from auditgym.config import (DEFAULT_CONFIG, DISTRACTORS, ERROR_TYPES, INTERACTIONS, MAX_ATTEMPTS, MAX_DOCUMENTS,
                             MAX_ISSUES, PRESETS, ConfigError, document_bound, normalize_config)
from auditgym.datachecks import check_episode, check_records
from auditgym.export import load_episode_files, write_episode
from auditgym.generator import _build, generate
from auditgym.money import fx_id


def _range(rnd, lo, hi):
    u = rnd.random()
    if u < 0.3:
        return [lo, lo]
    if u < 0.6:
        return [hi, hi]
    return sorted([rnd.randint(lo, hi), rnd.randint(lo, hi)])


def random_config(rnd, i):
    """Случайный конфиг из опубликованных границ, с упором на края (20/120 счетов, 3/40 клиентов,
    0/200 фоновых операций, доля валют 0/1, ошибки до MAX_ISSUES, отвлекающие случаи до 12)."""
    errs, inter = {t: 0 for t in ERROR_TYPES}, {t: 0 for t in INTERACTIONS}
    keys = list(ERROR_TYPES) + list(INTERACTIONS)
    if rnd.random() < 0.3:
        keys = [rnd.choice(keys)]
    budget = rnd.choice([0, MAX_ISSUES, rnd.randint(0, MAX_ISSUES)])
    while budget > 0:
        k = rnd.choice(keys)
        if k in inter:
            if budget < 2:
                break
            inter[k] += 1
            budget -= 2
        else:
            errs[k] += 1
            budget -= 1
    dist = {k: rnd.choice([0, 1, 2, 4]) for k in DISTRACTORS}
    for k in rnd.sample(DISTRACTORS, 2):
        dist[k] = 12
    return {"errors": errs, "interactions": inter, "distractors": dist,
            "n_invoices": _range(rnd, 20, 120), "n_customers": _range(rnd, 3, 40),
            "background_entries": _range(rnd, 0, 200), "foreign_share": rnd.choice([0, 1, 0.3]),
            "id_salt": "g%d" % i, "order_seed": i}


def assert_honoured(tc, ep, cfg):
    """Эпизод выполняет конфиг точно и записывает фактические числа в provenance.actual."""
    n = normalize_config(cfg)
    act = ep.provenance["actual"]
    tc.assertEqual(act["distractors"], n["distractors"])
    tc.assertEqual(act["errors"], n["errors"])
    tc.assertEqual(act["interactions"], n["interactions"])
    tc.assertEqual(len(ep.truth["issues"]), sum(n["errors"].values()) + 2 * sum(n["interactions"].values()))
    tc.assertTrue(n["n_customers"][0] <= act["customers"] <= n["n_customers"][1])
    tc.assertTrue(n["background_entries"][0] <= act["background_entries"] <= n["background_entries"][1])
    docs = ep.public["manifest"]["counts"]["documents"]
    tc.assertEqual(act["documents"], docs)
    tc.assertLessEqual(docs, MAX_DOCUMENTS)
    tc.assertLessEqual(docs, document_bound(n) + 2 * act["regular_invoices"])  # граница строгая
    tc.assertEqual(check_episode(ep), [], cfg)


class AcceptedConfigsAlwaysGenerate(unittest.TestCase):
    # Конфиги, на которых прежний генератор падал (30/30, 29/30, 6/40 seed).
    FORMERLY_FAILING = [
        {"n_invoices": [20, 20], "interactions": {"masked_duplicate": 6}, "distractors": {"unremitted_payment": 12}},
        {"n_invoices": [20, 20], "errors": {"payment_allocation": 12}, "distractors": {"unremitted_payment": 12}},
        {"interactions": {"masked_duplicate": 6}, "distractors": {"split_payment": 12, "multi_invoice_payment": 12}},
    ]

    def test_formerly_failing_configs(self):
        for cfg in self.FORMERLY_FAILING:
            for seed in range(4):
                assert_honoured(self, generate(seed, cfg), cfg)

    def test_small_fuzz_of_accepted_configs(self):
        rnd, done = random.Random(20261120), 0
        for i in range(60):
            cfg = random_config(rnd, i)
            try:
                normalize_config(cfg)
            except ConfigError:
                continue
            assert_honoured(self, generate(880000 + i, cfg), cfg)
            done += 1
        self.assertGreater(done, 30)

    def test_guaranteed_last_attempts(self):
        # у предела документов последние попытки берут столько обычных счетов, сколько гарантированно помещается
        cfg = {"errors": {"fx_rate": 12}, "foreign_share": 0, "n_invoices": [120, 120],
               "distractors": dict({k: 0 for k in DISTRACTORS}, recurring_amount=12, same_day_repeat=12,
                                   split_payment=6, refund=6)}
        n = normalize_config(cfg)
        self.assertGreater(document_bound(n), 180)
        for attempt in (MAX_ATTEMPTS - 1, MAX_ATTEMPTS - 4):
            for seed in range(3):
                ep = _build(seed, n, attempt)
                self.assertLessEqual(ep.public["manifest"]["counts"]["documents"], MAX_DOCUMENTS)
                self.assertEqual(check_episode(ep), [])
        assert_honoured(self, generate(5, cfg), cfg)


class ImpossibleConfigsRejected(unittest.TestCase):
    # Самые тяжёлые допустимые конфиги: все отвлекающие случаи по 12 и MAX_ISSUES самых дорогих ошибок.
    HEAVIEST = [
        {"distractors": {k: 12 for k in DISTRACTORS}, "errors": {"fx_rate": MAX_ISSUES}},
        {"distractors": {k: 12 for k in DISTRACTORS}, "interactions": {"masked_duplicate": MAX_ISSUES // 2}},
        {"distractors": {k: 12 for k in DISTRACTORS}, "interactions": {"partial_fx_boundary": MAX_ISSUES // 2}},
    ]

    def test_every_config_in_published_bounds_fits(self):
        # предел документов не отвергает ни одного конфига в опубликованных границах
        for cfg in self.HEAVIEST:
            self.assertLessEqual(document_bound(normalize_config(cfg)), MAX_DOCUMENTS, cfg)
        for cfg in self.HEAVIEST[:2]:
            assert_honoured(self, generate(1, cfg), cfg)

    def test_too_many_documents_is_config_error(self):
        # проверка предела сама по себе работает (предел временно ниже границы конфига)
        cfg = {"distractors": {k: 12 for k in DISTRACTORS}}
        with mock.patch.object(config_module, "MAX_DOCUMENTS", document_bound(normalize_config(cfg)) - 1):
            with self.assertRaises(ConfigError) as cm:
                normalize_config(cfg)
            self.assertIn("документ", str(cm.exception))
            with self.assertRaises(ConfigError):
                generate(1, cfg)

    def test_presets_and_default_fit(self):
        self.assertLessEqual(document_bound(normalize_config(None)), MAX_DOCUMENTS)
        for name in PRESETS:
            self.assertLessEqual(document_bound(normalize_config({"preset": name})), MAX_DOCUMENTS, name)
        # каждая ошибка одного типа до MAX_ISSUES помещается при отвлекающих случаях по умолчанию
        for t in ERROR_TYPES:
            normalize_config({"errors": {t: MAX_ISSUES}})
        for t in INTERACTIONS:
            normalize_config({"interactions": {t: MAX_ISSUES // 2}})
        self.assertEqual(DEFAULT_CONFIG["distractors"], normalize_config(None)["distractors"])


class ConfigHonoured(unittest.TestCase):
    def test_heavy_distractors_built_exactly(self):
        cfg = {"distractors": {"refund": 12, "partial_open": 12, "split_payment": 12, "unremitted_payment": 12},
               "n_invoices": [20, 20]}
        for seed in range(3):
            ep = generate(seed, cfg)
            assert_honoured(self, ep, cfg)
            pays = ep.public["payments"]
            self.assertEqual(sum(1 for p in pays if p["amount_minor"] < 0), 12)            # возвраты
            self.assertEqual(sum(1 for p in pays if p["invoice_refs"] is None), 12)        # без расписки
            self.assertEqual(len(ep.provenance["distractors"]["refund"]), 36)              # счёт + 2 платежа

    def test_foreign_share_zero_means_no_foreign_documents(self):
        for seed in range(6):
            ep = generate(seed, {"preset": "clean_hard", "foreign_share": 0})
            base = ep.public["manifest"]["base_currency"]
            self.assertEqual([d for d in ep.public["invoices"] + ep.public["payments"] if d["currency"] != base], [])
            self.assertEqual(ep.provenance["actual"]["foreign_customers"], 0)

    def test_foreign_share_zero_with_fx_errors_uses_one_foreign_customer(self):
        cfg = {"errors": {"fx_rate": 3}, "interactions": {"partial_fx_boundary": 1}, "foreign_share": 0}
        for seed in range(4):
            ep = generate(seed, cfg)
            self.assertEqual(ep.provenance["actual"]["foreign_customers"], 1)
            assert_honoured(self, ep, cfg)

    def test_foreign_share_one(self):
        ep = generate(3, {"preset": "mixed", "foreign_share": 1})
        act = ep.provenance["actual"]
        self.assertEqual(act["foreign_customers"], act["customers"])
        assert_honoured(self, ep, {"preset": "mixed", "foreign_share": 1})


class NoZeroRows(unittest.TestCase):
    def test_missing_allocation_writes_no_empty_1200_row(self):
        seen = 0
        for seed in range(40):
            ep = generate(seed, {"errors": {"payment_allocation": 3}})
            self.assertFalse([r for r in ep.public["ledger"] if r["debit_minor"] == 0 and r["credit_minor"] == 0])
            for m in ep.provenance["mutations"]:
                if m.get("variant") != "missing":
                    continue
                seen += 1
                pid = m["document"]["id"]
                rows = [r for r in ep.public["ledger"] if r["payment_id"] == pid]
                accounts = sorted(r["account"][:4] for r in rows)
                self.assertNotIn("1200", accounts)       # строки 1200 нет вовсе
                self.assertIn("2900", accounts)
                pay = next(p for p in ep.public["payments"] if p["payment_id"] == pid)
                cash = next(r for r in rows if r["account"].startswith("1000"))
                self.assertEqual(cash["invoice_id"], pay["invoice_refs"][0]["invoice_id"])  # ссылка на счёт сохранена
            self.assertEqual(check_episode(ep), [])
        self.assertGreater(seen, 5)

    def test_zero_row_is_a_book_violation(self):
        ep = generate(1, {})
        pub = copy.deepcopy(ep.public)
        row = dict(pub["ledger"][0], row_id="L-ZERO0001", debit_minor=0, credit_minor=0)
        pub["ledger"].append(row)
        self.assertTrue(any("нулевые" in v for v in book_invariants(pub, subledger=False)))
        self.assertIn("zero_rows", ep.public["policy"]["posting_rules"])


class FxIdRule(unittest.TestCase):
    def test_fx_id_template_in_manifest_and_policy_matches_file(self):
        ep = generate(8, {"errors": {"fx_rate": 3}, "interactions": {"partial_fx_boundary": 1}})
        fmt = ep.public["manifest"]["fx_id_format"]
        self.assertEqual(fmt["template"], "FX-{date}-{source_currency}-{target_currency}")
        self.assertEqual(fmt["fields"], ["date", "source_currency", "target_currency"])
        self.assertEqual(ep.public["policy"]["fx"]["fx_id_template"], fmt["template"])
        with tempfile.TemporaryDirectory() as d:
            write_episode(ep, Path(d))
            pdir = Path(d) / "public" / ep.episode_id
            with (pdir / "fx.csv").open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(list(rows[0]), ["date", "source_currency", "target_currency", "rate"])  # колонки по ТЗ
            # независимая сборка ID только из manifest и fx.csv
            ids = {fmt["template"].format(**{k: r[k] for k in fmt["fields"]}) for r in rows}
            ev = {e for t in ep.truth["issues"] for es in t["evidence_sets"] for e in es if e.startswith("FX-")}
            self.assertTrue(ev)
            self.assertLessEqual(ev, ids)
            self.assertEqual(ids, {f["fx_id"] for f in ep.public["fx"]})
            self.assertEqual(check_records(*load_episode_files(Path(d), ep.episode_id)), [])
        for f in ep.public["fx"]:
            self.assertEqual(f["fx_id"], fx_id(f["date"], f["source_currency"], f["target_currency"]))


if __name__ == "__main__":
    unittest.main()
