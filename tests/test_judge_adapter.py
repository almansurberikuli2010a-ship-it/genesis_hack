"""Официальные определения проверок жюри через judge_adapter.py.

* Блок 1 — контрактные тесты C01–C20 на фикстурах C (чистый эпизод), E (одна ошибка),
  L (таблица ≥ 101 записи) и U (ID, которого нет в открытых данных);
* Блок 3 — двадцать тестов matcher и награды (общая фикстура: одна ошибка A типа T, запись R1,
  10000 KZT, доказательства {D1, D2}, τ = 0) и открытые примеры M01–M12;
* базовые политики через адаптер: oracle — 1 − 0.002·шаги, flag_all — плохо.
Парные проверки P01–P20 — в tests/test_judge_pairs.py.
"""
import json
import shutil
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

import judge_adapter as J
from auditgym.export import FLAT_FILES, load_public_dir
from auditgym.generator import generate
from auditgym.matcher import FINDING_TYPES, select_pairs

ROOT_TMP = None
C = E = KZT = None
U = None
TRUTH_KEYS = {"issue_id", "issues", "expected_amount_minor", "evidence_sets", "mutation_id", "mutations", "story_hash",
              "tolerance_minor", "ledger_diff", "canonical_ids", "documents_added", "seed", "provenance", "truth"}


def setUpModule():
    global ROOT_TMP, C, E, KZT, U
    J._log.disabled = True  # журнал оценщика проверяется через J.GRADER_LOG; без вывода в stderr тестов
    ROOT_TMP = Path(tempfile.mkdtemp(prefix="auditgym-judge-"))
    C, E = ROOT_TMP / "C", ROOT_TMP / "E"
    J.generate_episode(21, {"preset": "clean"}, str(C))
    J.generate_episode(22, {"errors": {"duplicate_invoice": 1}}, str(E))
    seed = next(s for s in range(1, 400) if generate(s, {"preset": "clean"}).public["manifest"]["base_currency"] == "KZT")
    KZT = ROOT_TMP / "KZT"
    J.generate_episode(seed, {"preset": "clean"}, str(KZT))
    blob = "".join((d / n).read_text(encoding="utf-8") for d in (C, E, KZT) for n in FLAT_FILES[:6])
    U = next(x for x in ("U-%d" % i for i in range(1000)) if x not in blob)  # выбран после чтения данных


def tearDownModule():
    J._log.disabled = False
    shutil.rmtree(ROOT_TMP, ignore_errors=True)


def pub(d):
    return load_public_dir(Path(d))


def keys(o, acc=None):
    acc = set() if acc is None else acc
    if isinstance(o, dict):
        for k, v in o.items():
            acc.add(k)
            keys(v, acc)
    elif isinstance(o, list):
        for v in o:
            keys(v, acc)
    return acc


def strings(o, acc=None):
    acc = set() if acc is None else acc
    if isinstance(o, str):
        acc.add(o)
    elif isinstance(o, dict):
        for k, v in o.items():
            acc.add(k)
            strings(v, acc)
    elif isinstance(o, list):
        for v in o:
            strings(v, acc)
    return acc


def closed_cfg(d):
    return {"mode": "closed", "episode_dir": str(d), "team_config": {}}


def act(name, **args):
    return {"name": name, "args": args}


def page_all(env, table, limit=50):
    """Все записи таблицы через курсоры; → (записи, страницы)."""
    out, pages, cur = [], [], None
    while True:
        o = env.step(act("list_records", table=table, cursor=cur, limit=limit))[0]
        pages.append(o)
        out += o["result"]["records"]
        cur = o["result"]["next_cursor"]
        if cur is None:
            return out, pages


# ================================================================== Блок 1: C01–C20

class Block1Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = J.make_env()

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def reset(self, d=None):
        return self.env.reset(0, closed_cfg(d or C))

    def test_C01_reset_returns_json_pair(self):
        out = self.reset()
        self.assertIsInstance(out, tuple)
        self.assertEqual(len(out), 2)
        obs, info = out
        self.assertIsInstance(obs, dict)
        self.assertIsInstance(info, dict)
        json.dumps(out, allow_nan=False)
        o = self.env.step(act("list_records", table="ledger", cursor=None, limit=5))
        json.dumps(o, allow_nan=False)

    def test_C02_first_observation(self):
        obs, info = self.reset(E)
        self.assertEqual(set(obs), {"task", "manifest", "policy", "remaining_steps"})
        self.assertIsInstance(obs["task"], str)
        self.assertEqual(obs["remaining_steps"], 100)
        self.assertFalse(keys([obs, info]) & TRUTH_KEYS)
        text = json.dumps([obs, info], ensure_ascii=False)
        truth = json.loads((E / "truth.json").read_text(encoding="utf-8"))
        self.assertTrue(truth["issues"])
        for t in truth["issues"]:
            self.assertNotIn(t["issue_id"], text)
        self.assertNotIn(str(E), text)

    def test_C03_manifest(self):
        obs, _ = self.reset()
        m = obs["manifest"]
        for k in ("schema_version", "episode_id", "base_currency", "period_start", "period_end", "timezone", "counts",
                  "rounding"):
            self.assertIn(k, m)
        rows = {t: page_all(self.env, t)[0] for t in J.TABLES}
        p = pub(C)
        for t in J.TABLES:
            self.assertEqual(m["counts"][t], len(rows[t]), t)
            self.assertEqual(len(rows[t]), len(p[t]), t)
        self.assertEqual(m["counts"]["documents"], len(rows["invoices"]) + len(rows["payments"]))
        self.assertEqual(m["episode_id"], p["manifest"]["episode_id"])

    def test_C04_step_tuple_types(self):
        self.reset()
        out = self.env.step(act("list_records", table="ledger", cursor=None, limit=50))
        self.assertEqual(len(out), 5)
        obs, r, te, tr, info = out
        self.assertIs(type(te), bool)
        self.assertIs(type(tr), bool)
        self.assertIsInstance(r, (int, float))
        self.assertEqual(set(obs), {"result", "error", "remaining_steps"})
        self.assertEqual(obs["remaining_steps"], 99)
        self.assertEqual(set(obs["result"]), {"records", "next_cursor"})
        json.dumps(out, allow_nan=False)

    def test_C05_pagination_on_L(self):
        self.reset()  # L: таблица ledger фикстуры C
        truth_rows = pub(C)["ledger"]
        self.assertGreaterEqual(len(truth_rows), 101)
        recs, pages = page_all(self.env, "ledger", 50)
        self.assertGreaterEqual(len(pages), 3)
        self.assertTrue(all(len(p["result"]["records"]) <= 50 for p in pages))
        ids = [r["row_id"] for r in recs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(ids), sorted(r["row_id"] for r in truth_rows))
        self.assertEqual(recs, truth_rows)  # все исходные строки, в порядке файла
        _, small = page_all(self.env, "ledger", 1000)  # limit выше 50 обрезается
        self.assertTrue(all(len(p["result"]["records"]) <= 50 for p in small))

    def test_C06_same_cursor_same_page(self):
        self.reset()
        first = self.env.step(act("list_records", table="ledger", cursor=None, limit=50))[0]
        cur = first["result"]["next_cursor"]
        a = self.env.step(act("list_records", table="ledger", cursor=cur, limit=50))[0]
        b = self.env.step(act("list_records", table="ledger", cursor=cur, limit=50))[0]
        self.assertEqual(a["result"], b["result"])
        self.assertEqual((a["remaining_steps"], b["remaining_steps"]), (98, 97))

    def test_C07_get_record(self):
        self.reset()
        p = pub(C)
        r1 = p["ledger"][7]
        o = self.env.step(act("get_record", table="ledger", id=r1["row_id"]))[0]
        self.assertIsNone(o["error"])
        self.assertEqual(o["result"], r1)
        self.assertEqual(set(o["result"]), {"row_id", "timestamp", "account", "debit_minor", "credit_minor", "currency",
                                            "entity_id", "invoice_id", "payment_id"})
        inv, fx = p["invoices"][0], p["fx"][0]
        self.assertEqual(self.env.step(act("get_record", table="invoices", id=inv["invoice_id"]))[0]["result"], inv)
        self.assertEqual(self.env.step(act("get_record", table="fx", id=fx["fx_id"]))[0]["result"], fx)

    def test_C08_get_document(self):
        self.reset()
        p = pub(C)
        for d1 in (p["invoices"][3], p["payments"][2]):
            o = self.env.step(act("get_document", doc_id=d1["doc_id"]))[0]
            self.assertIsNone(o["error"])
            self.assertEqual(o["result"], d1)
        for path in ("/etc/passwd", "../truth.json", str(C / "truth.json"), "truth.json"):
            o = self.env.step(act("get_document", doc_id=path))[0]
            self.assertIsNone(o["result"])
            self.assertEqual(o["error"]["code"], "not_found")

    def test_C09_calculator_decimal(self):
        self.reset()
        o = self.env.step(act("calculator", expression="0.1+0.2"))[0]
        self.assertIsNone(o["error"])
        self.assertIsInstance(o["result"], str)
        self.assertEqual(Decimal(o["result"]), Decimal("0.3"))
        self.assertEqual(o["result"], "0.3")

    def test_C10_calculator_rejects_code(self):
        self.reset()
        marker = ROOT_TMP / "calc_marker"
        for expr in ("open('/etc/passwd').read()", "__import__('os').system('touch %s')" % marker,
                     "open('%s').read()" % (C / "truth.json"), "2**10**10", "[x for x in ()]", "1;2"):
            o = self.env.step(act("calculator", expression=expr))[0]
            self.assertIsNone(o["result"])
            self.assertEqual(o["error"]["code"], "calculator_error")
            self.assertNotIn("root:", json.dumps(o))
        self.assertFalse(marker.exists())

    def test_C11_get_record_unknown_id(self):
        self.reset()
        o = self.env.step(act("get_record", table="ledger", id=U))[0]
        self.assertIsNone(o["result"])
        self.assertEqual(o["error"]["code"], "not_found")
        self.assertEqual(o["remaining_steps"], 99)
        self.assertNotIn(U, o["error"]["message"])
        o = self.env.step(act("get_record", table="ledger", id=pub(C)["ledger"][0]["row_id"]))[0]
        self.assertIsNone(o["error"])  # процесс среды жив

    def test_C12_get_document_unknown_id(self):
        self.reset()
        o = self.env.step(act("get_document", doc_id=U))[0]
        self.assertIsNone(o["result"])
        self.assertEqual(o["error"]["code"], "not_found")
        self.assertEqual(o["remaining_steps"], 99)
        text = json.dumps(o, ensure_ascii=False)
        self.assertFalse(any(d["doc_id"] in text for d in pub(C)["invoices"] + pub(C)["payments"]))

    def test_C13_unknown_action(self):
        self.reset()
        for bad in (act("delete_records", table="ledger"), act("shell", cmd="ls"), {"name": 5, "args": {}},
                    {"type": "list_records", "table": "ledger"}, {"name": "list_records", "args": [], "x": 1}, "не JSON",
                    None, act("get_record", type="x", table="ledger", id="y"), {"name": "calculator", "args": "1+1"}):
            before = self.env.step(act("calculator", expression="1"))[0]["remaining_steps"]
            o, r, te, tr, info = self.env.step(bad)
            self.assertIsNone(o["result"])
            self.assertIsInstance(o["error"]["code"], str)
            self.assertLess(len(o["error"]["message"]), 120)
            self.assertEqual(o["remaining_steps"], before - 1)
            self.assertEqual((r, te, tr, info), (0.0, False, False, {}))

    def test_C14_error_does_not_give_free_step(self):
        self.reset()
        rid = pub(C)["ledger"][5]["row_id"]
        a = self.env.step(act("get_record", table="ledger", id=rid))[0]
        b = self.env.step(act("get_record", table="ledger", id=U))[0]
        c = self.env.step(act("get_record", table="ledger", id=rid))[0]
        self.assertEqual([x["remaining_steps"] for x in (a, b, c)], [99, 98, 97])
        self.assertEqual(a["result"], c["result"])
        self.assertIsNotNone(b["error"])

    def test_C15_submit_is_final(self):
        obs, _ = self.reset(E)
        findings = J.oracle_findings(E)
        o, r, te, tr, info = self.env.step(act("submit", findings=findings))
        self.assertEqual((o["result"], o["error"], te, tr), ({"accepted": True}, None, True, False))
        first = self.env.grader_report()["result"]
        for later in (act("submit", findings={"episode_id": obs["manifest"]["episode_id"], "findings": []}),
                      act("list_records", table="ledger", cursor=None, limit=5)):
            o2, r2, te2, tr2, info2 = self.env.step(later)
            self.assertEqual(o2["error"]["code"], "episode_finished")
            self.assertEqual((r2, info2), (0.0, {}))
        self.assertEqual(self.env.grader_report()["result"], first)
        self.assertEqual((first["tp"], first["fp"], first["fn"], first["reward"]), (1, 0, 0, 1.0))

    def test_C16_submit_as_action_100(self):
        obs, _ = self.reset()
        for i in range(99):
            o, r, te, tr, _ = self.env.step(act("calculator", expression="1+1"))
            self.assertFalse(te or tr)
        self.assertEqual(o["remaining_steps"], 1)
        o, r, te, tr, _ = self.env.step(act("submit", findings={"episode_id": obs["manifest"]["episode_id"], "findings": []}))
        self.assertEqual((o["result"], te, tr, o["remaining_steps"]), ({"accepted": True}, True, False, 0))
        res = self.env.grader_report()["result"]
        self.assertEqual((res["inspection_steps"], res["end_reason"]), (99, "submit"))
        self.assertAlmostEqual(res["reward"], 1 - 0.002 * 99, delta=1e-9)

    def test_C17_step_limit_truncates(self):
        obs, _ = self.reset(E)
        for i in range(100):
            o, r, te, tr, _ = self.env.step(act("calculator", expression="1"))
            if i < 99:
                self.assertFalse(te or tr)
        self.assertEqual((te, tr, o["remaining_steps"]), (False, True, 0))
        res = self.env.grader_report()["result"]
        self.assertEqual((res["end_reason"], res["n_findings"], res["tp"]), ("step_limit", 0, 0))
        o, r, te, tr, _ = self.env.step(act("submit", findings=J.oracle_findings(E)))
        self.assertEqual(o["error"]["code"], "episode_finished")
        self.assertEqual(self.env.grader_report()["result"], res)
        self.assertEqual(J.score_episode(str(E), None, 100)["tp"], 0)  # пустой итоговый ответ

    def test_C18_time_limit_truncates(self):
        with J.make_env(time_limit_s=0.5) as env:
            obs, _ = env.reset(0, closed_cfg(E))
            env.step(act("calculator", expression="1"))
            time.sleep(0.7)  # «политика» думает дольше лимита
            o, r, te, tr, info = env.step(act("submit", findings=J.oracle_findings(E)))
            self.assertEqual((te, tr, o["result"], o["error"]["code"]), (False, True, None, "time_limit"))
            res = env.grader_report()["result"]
            self.assertEqual((res["end_reason"], res["n_findings"], res["tp"]), ("time_limit", 0, 0))
            o2 = env.step(act("submit", findings=J.oracle_findings(E)))[0]
            self.assertEqual(o2["error"]["code"], "episode_finished")
            self.assertEqual(env.grader_report()["result"], res)

    def test_C19_closed_mode_hides_reward(self):
        obs, info = self.reset(E)
        seen = [obs, info]
        for a in (act("list_records", table="invoices", cursor=None, limit=50), act("get_record", table="ledger", id=U),
                  act("calculator", expression="2*3")):
            o, r, te, tr, inf = self.env.step(a)
            self.assertEqual((r, inf), (0.0, {}))
            self.assertIs(type(r), float)
            seen += [o, inf]
        o, r, te, tr, inf = self.env.step(act("submit", findings=J.oracle_findings(E)))
        self.assertEqual((o, r, te, inf), ({"result": {"accepted": True}, "error": None, "remaining_steps": 96}, 0.0, True, {}))
        seen += [o, inf]
        res = self.env.grader_report()["result"]
        self.assertAlmostEqual(res["reward"], 1 - 0.002 * 3, delta=1e-9)
        self.assertEqual(J.score_episode(str(E), J.oracle_findings(E), 3)["return"], res["reward"])
        self.assertFalse(keys(seen) & TRUTH_KEYS)
        # в train терминальная награда доступна политике
        with J.make_env() as env:
            env.reset(0, {"mode": "train", "episode_dir": str(E)})
            env.step(act("calculator", expression="1"))
            o, r, te, tr, inf = env.step(act("submit", findings=J.oracle_findings(E)))
            self.assertAlmostEqual(r, 1 - 0.002, delta=1e-9)
            self.assertEqual(inf["grading"]["f1"], 1.0)

    def test_C20_no_leak_through_any_channel(self):
        seen = []
        for d in (C, E):
            p = pub(d)
            truth = json.loads((d / "truth.json").read_text(encoding="utf-8"))
            prov = json.loads((d / "provenance.json").read_text(encoding="utf-8"))
            docs = [x["doc_id"] for x in p["invoices"] + p["payments"]]
            obs, info = self.reset(d)
            seen += [obs, info]
            for t in J.TABLES:
                seen += page_all(self.env, t)[1]
            probes = [act("get_record", table="ledger", id="../truth.json"), act("get_record", table="truth", id="x"),
                      act("list_records", table="provenance", cursor=None, limit=50), act("get_document", doc_id="/etc/passwd"),
                      act("get_record", table="ledger", id="1 OR 1=1"), act("shell", cmd="cat truth.json"),
                      act("sql", query="select * from truth"), act("calculator", expression="open('truth.json')"),
                      act("list_records", table="ledger", cursor="../../", limit=50),
                      act("submit", findings={"episode_id": "нет", "findings": []})]
            for a in probes:
                o, r, te, tr, inf = self.env.step(a)
                seen += [o, inf]
                if te or tr:
                    break
            for i in range(0, len(docs), 98):  # каждый документ через get_document
                self.reset(d)
                for did in docs[i:i + 98]:
                    o, r, te, tr, inf = self.env.step(act("get_document", doc_id=did))
                    self.assertEqual(o["result"]["doc_id"], did)
                    seen += [o, inf]
            # строки (ключи и значения), которые есть только в закрытых файлах, не встречаются в наблюдениях
            open_text = "".join((d / n).read_text(encoding="utf-8") for n in FLAT_FILES[:6]) + obs["task"]
            closed = {s for s in strings([truth, prov]) if len(s) >= 6 and s not in open_text}
            self.assertTrue(closed)
            text = json.dumps(seen, ensure_ascii=False)
            leaked = sorted(s for s in closed if s in text)
            self.assertEqual(leaked, [])
            self.assertNotIn(str(d), text)
        self.assertFalse(keys(seen) & TRUTH_KEYS)
        for o in seen:
            if isinstance(o, dict) and o.get("error"):
                self.assertLess(len(o["error"]["message"]), 120)
                self.assertNotIn(str(ROOT_TMP), o["error"]["message"])
                self.assertNotIn("truth", o["error"]["message"])


# ================================================================== Блок 3: 20 тестов matcher и награды

class _Fixture:
    """Общая фикстура Блока 3: эталон с ошибкой A (тип T = fx_rate, запись R1, 10000 KZT, {D1, D2}, τ = 0)."""

    T, OTHER_T = "fx_rate", "period_cutoff"

    def __init__(self, name, issues_fn, tau=None):
        self.dir = ROOT_TMP / name
        if self.dir.exists():
            shutil.rmtree(self.dir)
        shutil.copytree(KZT, self.dir)
        p = pub(self.dir)
        self.eid = p["manifest"]["episode_id"]
        inv = p["invoices"]
        self.R = [i["invoice_id"] for i in inv[:5]]
        self.D1, self.D2 = inv[0]["doc_id"], p["ledger"][0]["row_id"]
        self.D3, self.D4 = inv[1]["doc_id"], p["ledger"][1]["row_id"]
        self.pay = p["payments"][0]
        issues = issues_fn(self)
        truth = {"episode_id": self.eid, "schema_version": p["manifest"]["schema_version"], "base_currency": "KZT",
                 "issues": issues}
        (self.dir / "truth.json").write_text(json.dumps(truth, ensure_ascii=False), encoding="utf-8")
        if tau is not None:
            pol = json.loads((self.dir / "policy.json").read_text(encoding="utf-8"))
            pol["matching"]["amount_tolerance_minor"][issues[0]["type"]] = tau
            (self.dir / "policy.json").write_text(json.dumps(pol, ensure_ascii=False), encoding="utf-8")

    def issue(self, iid="ISS-A", t=None, affected=None, amount=10000, ev=None, tau=0):
        return {"issue_id": iid, "type": t or self.T, "affected_ids": affected or [self.R[0]],
                "expected_amount_minor": amount, "currency": "KZT", "tolerance_minor": tau,
                "evidence_sets": ev or [[self.D1, self.D2]], "mutation_id": "M01"}

    def F(self, **kw):
        f = {"type": self.T, "affected_ids": [self.R[0]], "claimed_amount_minor": 10000, "currency": "KZT",
             "evidence_ids": [self.D1, self.D2], "explanation": "проведено не по курсу политики"}
        f.update(kw)
        return f

    def score(self, findings, steps=10):
        return J.score_episode(str(self.dir), {"episode_id": self.eid, "findings": findings}, steps)


class Block3MatcherReward(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = _Fixture("b3_A", lambda f: [f.issue()])
        cls.empty = _Fixture("b3_empty", lambda f: [])
        cls.ab = _Fixture("b3_AB", lambda f: [f.issue(), f.issue("ISS-B", affected=[f.R[1]], amount=5000, ev=[[f.D3, f.D4]])])

    def check(self, r, tp, fp, fn, p, rec, f1, ret):
        self.assertEqual((r["tp"], r["fp"], r["fn"]), (tp, fp, fn))
        for k, v in (("precision", p), ("recall", rec), ("f1", f1), ("return", ret)):
            self.assertAlmostEqual(r[k], v, delta=1e-9, msg=k)

    def test_01_correct(self):
        self.check(self.a.score([self.a.F()]), 1, 0, 0, 1, 1, 1, 0.98)

    def test_02_silence_on_error(self):
        self.check(self.a.score([]), 0, 0, 1, 0, 0, 0, -1)

    def test_03_empty_truth_empty_findings(self):
        self.check(self.empty.score([]), 0, 0, 0, 0, 0, 1, 0.98)

    def test_04_empty_truth_false_finding(self):
        self.check(self.empty.score([self.empty.F()]), 0, 1, 0, 0, 0, 0, -1)

    def test_05_duplicate_finding(self):
        r = self.a.score([self.a.F(), self.a.F()])
        self.check(r, 1, 1, 0, 1 / 2, 1, 2 / 3, 47 / 150)
        self.assertLessEqual(r["return"], self.a.score([self.a.F()])["return"])

    def test_06_wrong_type(self):
        self.check(self.a.score([self.a.F(type=self.a.OTHER_T)]), 0, 1, 1, 0, 0, 0, -1)

    def test_07_other_existing_record(self):
        self.check(self.a.score([self.a.F(affected_ids=[self.a.R[1]])]), 0, 1, 1, 0, 0, 0, -1)

    def test_08_amount_off_by_one_tau_zero(self):
        self.check(self.a.score([self.a.F(claimed_amount_minor=10001)]), 0, 1, 1, 0, 0, 0, -1)

    def test_09_other_currency(self):
        self.check(self.a.score([self.a.F(currency="EUR")]), 0, 1, 1, 0, 0, 0, -1)

    def test_10_partial_evidence(self):
        self.check(self.a.score([self.a.F(evidence_ids=[self.a.D1])]), 0, 1, 1, 0, 0, 0, -1)

    def test_11_unknown_evidence(self):
        self.check(self.a.score([self.a.F(evidence_ids=[self.a.D1, U])]), 0, 1, 1, 0, 0, 0, -1)

    def test_12_flag_everything(self):
        fs = [self.a.F(type=t, affected_ids=[r]) for r in self.a.R for t in FINDING_TYPES]
        self.assertEqual(len(fs), 20)
        self.check(self.a.score(fs), 1, 19, 0, 1 / 20, 1, 2 / 21, -871 / 1050)

    def test_13_two_issues_one_found(self):
        self.check(self.ab.score([self.ab.F()]), 1, 0, 1, 1, 1 / 2, 2 / 3, 47 / 150)

    def test_14_two_issues_one_found_one_false(self):
        self.check(self.ab.score([self.ab.F(), self.ab.F(affected_ids=[self.ab.R[4]])]), 1, 1, 1, 1 / 2, 1 / 2, 1 / 2, -0.02)

    def test_15_zero_steps(self):
        self.check(self.a.score([self.a.F()], 0), 1, 0, 0, 1, 1, 1, 1.0)

    def test_16_fifty_steps(self):
        self.check(self.a.score([self.a.F()], 50), 1, 0, 0, 1, 1, 1, 0.90)
        self.assertAlmostEqual(self.a.score([self.a.F()], 10)["return"] - self.a.score([self.a.F()], 50)["return"], 0.08,
                               delta=1e-9)

    def test_17_hundred_steps(self):
        self.check(self.a.score([self.a.F()], 100), 1, 0, 0, 1, 1, 1, 0.80)

    def _structural(self, findings):
        n = len(J.GRADER_LOG)
        r = self.a.score(findings)
        self.assertEqual(r, {"tp": None, "fp": None, "fn": None, "precision": None, "recall": None, "f1": None, "return": -1.0})
        self.assertEqual(len(J.GRADER_LOG), n + 1)
        self.assertEqual(J.GRADER_LOG[-1]["event"], "invalid_submission")
        with J.make_env() as env:  # тот же ответ в эпизоде: конец с −1, ошибка — у оценщика
            env.reset(0, closed_cfg(self.a.dir))
            o, rew, te, tr, info = env.step(act("submit", findings={"episode_id": self.a.eid, "findings": findings}))
            self.assertEqual((rew, te, tr, info, o["result"], o["error"]["code"]), (0.0, True, False, {}, None, "invalid_submission"))
            rep = env.grader_report()
            self.assertEqual((rep["result"]["valid"], rep["result"]["reward"]), (False, -1.0))
            self.assertEqual(rep["log"][0]["event"], "invalid_submission")

    def test_18_31_findings(self):
        self._structural([self.a.F()] * 31)

    def test_19_11_evidence_ids(self):
        p = pub(self.a.dir)
        ev = [self.a.D1, self.a.D2] + [r["row_id"] for r in p["ledger"][2:11]]
        self.assertEqual(len(set(ev)), 11)
        self._structural([self.a.F(evidence_ids=ev)])

    def test_20_explanation_1001(self):
        self._structural([self.a.F(explanation="x" * 1001)])
        self.check(self.a.score([self.a.F(explanation="x" * 1000)]), 1, 0, 0, 1, 1, 1, 0.98)
        self.check(self.a.score([self.a.F(explanation="я" * 1000)]), 1, 0, 0, 1, 1, 1, 0.98)  # кодовые точки, не байты


# ================================================================== открытые примеры M01–M12

class OpenExamplesM(unittest.TestCase):
    """Ошибка A: affected_ids = {R1, R2} (payment_allocation: payment_id и invoice_id), 10000 KZT,
    доказательства {D1, D2}, τ = 0; D3 — существующее дополнительное доказательство; U отсутствует."""

    @classmethod
    def setUpClass(cls):
        def issues(f):
            f.T, f.OTHER_T = "payment_allocation", "duplicate_invoice"
            f.R1, f.R2, f.R3 = f.pay["payment_id"], f.R[0], f.R[1]
            return [f.issue(t="payment_allocation", affected=[f.R1, f.R2], ev=[[f.D1, f.D2]])]
        cls.m = _Fixture("m_A", issues)
        cls.m8 = _Fixture("m_tau1", lambda f: [dict(issues(f)[0], tolerance_minor=1)], tau=1)
        for f in (cls.m, cls.m8):
            f.T, f.R1, f.R2, f.R3 = "payment_allocation", f.pay["payment_id"], f.R[0], f.R[1]

    def F(self, fx=None, **kw):
        fx = fx or self.m
        return fx.F(type="payment_allocation", **dict({"affected_ids": [fx.R1, fx.R2]}, **kw))

    def tpfpfn(self, r):
        return r["tp"], r["fp"], r["fn"]

    def test_baseline_correct(self):
        self.assertEqual(self.tpfpfn(self.m.score([self.F()])), (1, 0, 0))

    def test_M01_affected_set_with_repeats(self):
        self.assertEqual(self.tpfpfn(self.m.score([self.F(affected_ids=[self.m.R2, self.m.R1, self.m.R1])])), (1, 0, 0))

    def test_M02_extra_unaffected_record(self):
        self.assertEqual(self.tpfpfn(self.m.score([self.F(affected_ids=[self.m.R1, self.m.R2, self.m.R3])])), (0, 1, 1))

    def test_M03_extra_existing_evidence(self):
        self.assertEqual(self.tpfpfn(self.m.score([self.F(evidence_ids=[self.m.D2, self.m.D1, self.m.D3])])), (1, 0, 0))

    def test_M04_unknown_evidence_breaks_match(self):
        self.assertEqual(self.tpfpfn(self.m.score([self.F(evidence_ids=[self.m.D1, self.m.D2, U])])), (0, 1, 1))

    def test_M05_evidence_limit_before_dedup(self):
        r = self.m.score([self.F(evidence_ids=[self.m.D1] * 10 + [self.m.D2])])
        self.assertEqual((r["return"], r["tp"]), (-1.0, None))

    def test_M06_currency_exact(self):
        for cur in ("kzt", "Kzt", " KZT", "KZT ", "KZT\n"):
            self.assertEqual(self.tpfpfn(self.m.score([self.F(currency=cur)])), (0, 1, 1), cur)

    def test_M07_amount_tau_zero(self):
        self.assertEqual(self.tpfpfn(self.m.score([self.F(claimed_amount_minor=10001)])), (0, 1, 1))
        self.assertEqual(self.tpfpfn(self.m.score([self.F(claimed_amount_minor=9999)])), (0, 1, 1))

    def test_M08_published_tau_one(self):
        pol = json.loads((self.m8.dir / "policy.json").read_text(encoding="utf-8"))
        self.assertEqual(pol["matching"]["amount_tolerance_minor"]["payment_allocation"], 1)
        for amt, want in ((10001, (1, 0, 0)), (9999, (1, 0, 0)), (10002, (0, 1, 1)), (9998, (0, 1, 1))):
            self.assertEqual(self.tpfpfn(self.m8.score([self.F(self.m8, claimed_amount_minor=amt)])), want, amt)

    def test_M09_amount_must_be_json_integer(self):
        for bad in ("10000", True, 10000.0, None):
            r = self.m.score([self.F(claimed_amount_minor=bad)])
            self.assertEqual((r["return"], r["f1"]), (-1.0, None), repr(bad))

    def test_M10_maximum_matching_not_greedy(self):
        self.assertEqual(select_pairs([[0, 1], [0]], ["A", "B"]), [("A", 1), ("B", 0)])
        fx = _Fixture("m_10", lambda f: [f.issue("ISS-A", ev=[[f.D1]]), f.issue("ISS-B", ev=[[f.D2]])])
        r = fx.score([fx.F(evidence_ids=[fx.D1, fx.D2]), fx.F(evidence_ids=[fx.D1])])
        self.assertEqual(self.tpfpfn(r), (2, 0, 0))

    def test_M11_deterministic_tie_break(self):
        self.assertEqual(select_pairs([[0, 1], [0, 1]], ["A", "B"]), [("A", 0), ("B", 1)])
        self.assertEqual(select_pairs([[0, 1], [0, 1]], ["B", "A"]), [("A", 0), ("B", 1)])  # по issue_id, не по позиции
        self.assertEqual(select_pairs([[0, 1], [0, 1]], ["a", "B"]), [("B", 0), ("a", 1)])  # кодовые точки: "B" < "a"
        fx = _Fixture("m_11", lambda f: [f.issue("ISS-B", ev=[[f.D1]]), f.issue("ISS-A", ev=[[f.D1]])])
        f0, f1 = fx.F(), fx.F(explanation="второй")
        a, b = fx.score([f0, f1]), fx.score([f1, f0])
        self.assertEqual((self.tpfpfn(a), a["return"]), ((2, 0, 0), b["return"]))
        self.assertEqual(self.tpfpfn(b), (2, 0, 0))
        from auditgym.matcher import match, match_context
        truth = json.loads((fx.dir / "truth.json").read_text(encoding="utf-8"))
        m = match([f0, f1], truth["issues"], match_context(pub(fx.dir)))
        self.assertEqual(sorted((iid, i) for i, iid in m["pairs"]), [("ISS-A", 0), ("ISS-B", 1)])

    def test_M12_identical_findings_not_merged(self):
        r = self.m.score([self.F(), self.F()])
        self.assertEqual(self.tpfpfn(r), (1, 1, 0))
        self.assertAlmostEqual(r["return"], 47 / 150, delta=1e-9)


# ================================================================== базовые политики через адаптер

class _HangingPolicy:
    """Политика протокола жюри, которая «думает» дольше лимита времени эпизода."""

    def begin(self, obs):
        pass

    def act(self, obs):
        time.sleep(30)
        return {"name": "submit", "args": {"findings": {"episode_id": "x", "findings": []}}}


class BaselinesThroughAdapter(unittest.TestCase):
    def test_hanging_policy_stopped_at_time_limit(self):
        for isolated in (True, False):
            t0 = time.monotonic()
            r = J.run_on_episode(_HangingPolicy(), str(E), time_limit_s=2.0, isolated=isolated)
            self.assertLess(time.monotonic() - t0, 8.0, isolated)
            self.assertTrue(r["truncated"], isolated)
            self.assertEqual(r["env_result"]["end_reason"], "time_limit", isolated)
            self.assertEqual(r["score"]["return"], -1.0, isolated)

    def test_oracle_and_flag_all(self):
        for d in (C, E):
            for k in (0, 4):
                r = J.run_on_episode(J.OraclePolicy(str(d), k), str(d))
                self.assertEqual(r["inspection_steps"], k)
                self.assertAlmostEqual(r["score"]["return"], 1 - 0.002 * k, delta=1e-9)
                self.assertEqual(r["score"]["return"], r["env_result"]["reward"])
                self.assertEqual(set(r["policy_rewards"]), {0.0})
            fa = J.run_on_episode(J.baseline("flag_all"), str(d))
            self.assertLessEqual(fa["score"]["return"], -0.9)
            self.assertEqual(fa["score"]["return"], fa["env_result"]["reward"])
        ra = J.run_on_episode(J.baseline("rule_auditor"), str(E))
        self.assertAlmostEqual(ra["score"]["return"], 1 - 0.002 * ra["inspection_steps"], delta=1e-9)

    def test_generate_episode_writes_flat_files(self):
        self.assertEqual(sorted(p.name for p in C.iterdir()), sorted(FLAT_FILES))
        with tempfile.TemporaryDirectory() as d:
            J.generate_episode(21, {"mode": "train", "team_config": {"preset": "clean"}}, d)
            self.assertEqual({n: (Path(d) / n).read_text(encoding="utf-8") for n in FLAT_FILES},
                             {n: (C / n).read_text(encoding="utf-8") for n in FLAT_FILES})

    def test_train_mode_generates_from_seed_and_team_config(self):
        with J.make_env() as env:
            o1, i1 = env.reset(21, {"mode": "train", "team_config": {"preset": "clean"}})
            self.assertEqual(o1["manifest"], pub(C)["manifest"])
            o2, _ = env.reset(21, {"mode": "train", "team_config": {"preset": "clean"}})
            self.assertEqual(o1, o2)
            o3, _ = env.reset(0, {"mode": "closed", "episode_dir": str(E), "team_config": {"preset": "heavy"}})
            self.assertEqual(o3["manifest"], pub(E)["manifest"])  # episode_dir важнее: ровно этот эпизод
            with self.assertRaises(ValueError):
                env.reset(0, {"mode": "closed", "episode_dir": str(ROOT_TMP / "нет")})
            with self.assertRaises(ValueError):
                env.reset(0, {"mode": "debug", "episode_dir": str(E)})

    def test_reset_restarts_episode(self):
        with J.make_env() as env:
            env.reset(0, closed_cfg(E))
            env.step(act("submit", findings={"episode_id": pub(E)["manifest"]["episode_id"], "findings": []}))
            obs, _ = env.reset(0, closed_cfg(E))
            self.assertEqual(obs["remaining_steps"], 100)
            o = env.step(act("calculator", expression="1"))[0]
            self.assertEqual((o["result"], o["remaining_steps"]), ("1", 99))
            self.assertIsNone(env.grader_report()["result"])


if __name__ == "__main__":
    unittest.main()
