"""Матрица D01–D20 (Блок 2) через конфиг генератора и парные проверки P01–P20 (Блок 4).

Преобразование — как у жюри: копия плоского каталога эпизода, перестановка записей таблиц и/или
взаимно однозначная замена row_id, doc_id, invoice_id, payment_id, entity_id во всех данных и
ссылках, а также в закрытых truth.json и provenance.json (только значения, ключи объектов не
трогаются). Новые ID другого вида (цифры, точки, «#», пробел), ключ курса date:source:target
(fx_id) не меняется, порядок line_items и invoice_refs внутри документа сохраняется.
"""
import csv
import io
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

import judge_adapter as J
from auditgym.config import DISTRACTORS
from auditgym.datachecks import check_records
from auditgym.export import FLAT_FILES, LEDGER_COLS, load_flat_episode, load_public_dir

Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_rate": 0, "period_cutoff": 0}
NI = {"partial_fx_boundary": 0, "masked_duplicate": 0}
ND = {k: 0 for k in DISTRACTORS}


def _clean(**d):
    return {"errors": Z, "interactions": NI, "distractors": dict(ND, **d)}


def _err(t, n=1, variant=None, **extra):
    c = {"errors": dict(Z, **{t: n}), "interactions": NI, **extra}
    if variant:
        c["variants"] = {t: variant}
    return c


def _inter(t, n=1, **extra):
    return {"errors": Z, "interactions": dict(NI, **{t: n}), **extra}


# Сценарии матрицы Блока 2 — только документированными настройками генератора (auditgym/config.py).
D_MATRIX = [
    ("D01 обычная сверка", _clean()),
    ("D02 корректно разбитый платёж", _clean(split_payment=3)),
    ("D03 корректный возврат", _clean(refund=2)),
    ("D04 законные повторяющиеся суммы", _clean(recurring_amount=2, same_day_repeat=2)),
    ("D05 валютная/периодная граница", dict(_clean(tz_boundary=4, next_period_doc=2, prior_period_doc=1,
                                                  boundary_fx_partial=2), foreign_share=1.0)),
    ("D06 дубль счёта", _err("duplicate_invoice")),
    ("D07 дубль счёта", _err("duplicate_invoice", foreign_share=0.0)),
    ("D08 дубль счёта", _err("duplicate_invoice", 2)),
    ("D09 пропущенное распределение", _err("payment_allocation", 1, "missing")),
    ("D10 частичное распределение", _err("payment_allocation", 1, "partial")),
    ("D11 частичное распределение", _err("payment_allocation", 2, "partial")),
    ("D12 курс другой даты", _err("fx_rate", 1, "wrong_date")),
    ("D13 перевёрнутый курс", _err("fx_rate", 1, "inverted_rate")),
    ("D14 проведён поздно", _err("period_cutoff", 1, "prior_period")),
    ("D15 проведён задним числом", _err("period_cutoff", 1, "next_period")),
    ("D16 частичный валютный платёж на границе", _inter("partial_fx_boundary")),
    ("D17 частичный валютный платёж на границе", _inter("partial_fx_boundary", foreign_share=1.0)),
    ("D18 частичный валютный платёж на границе", _inter("partial_fx_boundary", 2)),
    ("D19 замаскированный дубль", _inter("masked_duplicate")),
    ("D20 замаскированный дубль", _inter("masked_duplicate", 2)),
]
D_SEED0 = 4100
TMP = None
SRC = {}


def setUpModule():
    global TMP
    TMP = Path(tempfile.mkdtemp(prefix="auditgym-pairs-"))
    for n, (name, cfg) in enumerate(D_MATRIX):
        SRC[n] = TMP / ("D%02d" % (n + 1))
        J.generate_episode(D_SEED0 + n, cfg, str(SRC[n]))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


# ------------------------------------------------------------------ преобразование жюри

def _read_csv(path):
    with path.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def _write_csv(path, cols, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8")


def _values(o, m):
    if isinstance(o, str):
        return m.get(o, o)
    if isinstance(o, list):
        return [_values(x, m) for x in o]
    if isinstance(o, dict):
        return {k: _values(v, m) for k, v in o.items()}
    return o


def jury_transform(src: Path, dst: Path, permute: bool, remap: bool, seed: int) -> dict:
    rnd = random.Random(seed)
    dst.mkdir(parents=True)
    lcols, ledger = _read_csv(src / "ledger.csv")
    fcols, fx = _read_csv(src / "fx.csv")
    inv = [json.loads(x) for x in (src / "invoices.jsonl").read_text(encoding="utf-8").splitlines() if x]
    pay = [json.loads(x) for x in (src / "payments.jsonl").read_text(encoding="utf-8").splitlines() if x]
    truth = json.loads((src / "truth.json").read_text(encoding="utf-8"))
    prov = json.loads((src / "provenance.json").read_text(encoding="utf-8"))
    m = {}
    if remap:
        nil = "null"
        spaces = {
            "%06d": [r["row_id"] for r in ledger],
            "d.%05d": [x["doc_id"] for x in inv + pay],
            "Invoice#%d": [x["invoice_id"] for x in inv] + [r["invoice_id"] for r in ledger if r["invoice_id"] != nil]
                          + [ref["invoice_id"] for p in pay for ref in p["invoice_refs"] or []],
            "p-%x": [x["payment_id"] for x in pay] + [r["payment_id"] for r in ledger if r["payment_id"] != nil],
            "E %04d": [x["entity_id"] for x in inv] + [r["entity_id"] for r in ledger if r["entity_id"] != nil],
        }
        for fmt, ids in spaces.items():
            uniq = sorted(set(ids))
            for old, num in zip(uniq, rnd.sample(range(10 ** 4, 10 ** 5), len(uniq))):
                m[old] = fmt % num
        assert len(set(m.values())) == len(m) and not set(m.values()) & set(m)
        for r in ledger:
            for k in ("row_id", "entity_id", "invoice_id", "payment_id"):
                if r[k] != nil:
                    r[k] = m[r[k]]
        for x in inv:
            x.update(doc_id=m[x["doc_id"]], invoice_id=m[x["invoice_id"]], entity_id=m[x["entity_id"]])
        for x in pay:
            x.update(doc_id=m[x["doc_id"]], payment_id=m[x["payment_id"]])
            for ref in x["invoice_refs"] or []:
                ref["invoice_id"] = m[ref["invoice_id"]]
        truth, prov = _values(truth, m), _values(prov, m)
    if permute:
        for t in (ledger, fx, inv, pay):
            rnd.shuffle(t)
    _write_csv(dst / "ledger.csv", lcols, ledger)
    _write_csv(dst / "fx.csv", fcols, fx)
    for name, rows in (("invoices.jsonl", inv), ("payments.jsonl", pay)):
        (dst / name).write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
    for name, obj in (("truth.json", truth), ("provenance.json", prov)):
        (dst / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    for name in ("manifest.json", "policy.json"):  # те же значения, другие пробелы
        (dst / name).write_text(json.dumps(json.loads((src / name).read_text(encoding="utf-8")), ensure_ascii=False),
                                encoding="utf-8")
    return m


def _canon_findings(findings, inv=None):
    """Выводы oracle как значения: множества ID (после обратного отображения ``inv``)."""
    back = (lambda x: inv.get(x, x)) if inv else (lambda x: x)
    return sorted((f["type"], tuple(sorted(back(a) for a in f["affected_ids"])), f["claimed_amount_minor"], f["currency"],
                   tuple(sorted(back(e) for e in f["evidence_ids"]))) for f in findings["findings"])


def act(name, **args):
    return {"name": name, "args": args}


# ------------------------------------------------------------------ Блок 2: матрица достижима конфигом

class MatrixD(unittest.TestCase):
    def test_D01_D20_generated_and_valid(self):
        for n, (name, cfg) in enumerate(D_MATRIX):
            pub, clean, truth, prov = load_flat_episode(SRC[n])
            self.assertEqual(check_records(pub, clean, truth, prov), [], name)
            types = sorted(t["type"] for t in truth["issues"])
            if n < 5:  # чистые компании: эталон пуст, заказанный отвлекающий случай есть
                self.assertEqual(types, [], name)
                for k, v in cfg["distractors"].items():
                    self.assertEqual(prov["actual"]["distractors"][k], v, name)
            for t, v in cfg.get("variants", {}).items():
                got = {mu["variant"] for mu in prov["mutations"] if mu["issue_type"] == t}
                self.assertEqual(got, {v}, name)
            for t, k in cfg["errors"].items():
                self.assertEqual(sum(1 for mu in prov["mutations"] if mu["issue_type"] == t), k, name)
            for s, k in cfg["interactions"].items():
                self.assertEqual(sum(1 for mu in prov["mutations"] if mu.get("scenario") == s), k, name)
        variants = {mu.get("variant") for n in (8, 9, 10) for mu in load_flat_episode(SRC[n])[3]["mutations"]}
        self.assertEqual(variants, {"missing", "partial"})  # D09–D11: и пропуск, и частичный случай


# ------------------------------------------------------------------ Блок 4: P01–P20

class PairsP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = J.make_env()

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def script(self, d):
        """Фиксированные действия (как повтор у жюри): чтение, ошибка, калькулятор, submit oracle."""
        p = load_public_dir(d)
        obs = [self.env.reset(0, {"mode": "closed", "episode_dir": str(d)})]
        for a in (act("list_records", table="ledger", cursor=None, limit=50), act("get_record", table="ledger", id="нет"),
                  act("get_document", doc_id=p["invoices"][0]["doc_id"]), act("calculator", expression="1/3"),
                  act("list_records", table="fx", cursor=None, limit=7), act("submit", findings=J.oracle_findings(str(d)))):
            obs.append(self.env.step(a))
        return json.dumps(obs, sort_keys=True, ensure_ascii=False), self.env.grader_report()["result"]

    def accessible(self, d, findings):
        """Каждый ID доказательств и затронутых записей правильного ответа доступен через инструменты."""
        refs = []
        for f in findings["findings"]:
            refs += [("aff", x) for x in f["affected_ids"]] + [("ev", x) for x in f["evidence_ids"]]
        p = load_public_dir(d)
        rows, docs, fxs = ({r["row_id"] for r in p["ledger"]}, {x["doc_id"] for x in p["invoices"] + p["payments"]},
                           {r["fx_id"] for r in p["fx"]})
        biz = {x["invoice_id"]: "invoices" for x in p["invoices"]} | {x["payment_id"]: "payments" for x in p["payments"]}
        for i in range(0, len(refs), 90):
            self.env.reset(0, {"mode": "closed", "episode_dir": str(d)})
            for kind, x in refs[i:i + 90]:
                if kind == "aff":
                    a = act("get_record", table=biz[x], id=x)
                elif x in rows:
                    a = act("get_record", table="ledger", id=x)
                elif x in docs:
                    a = act("get_document", doc_id=x)
                else:
                    self.assertIn(x, fxs)
                    a = act("get_record", table="fx", id=x)
                o = self.env.step(a)[0]
                self.assertIsNone(o["error"], x)

    def pair(self, n, permute, remap):
        src = SRC[n]
        dst = TMP / ("P%02d" % (n + 1))
        if dst.exists():
            shutil.rmtree(dst)
        m = jury_transform(src, dst, permute, remap, seed=777 + n)
        inv = {v: k for k, v in m.items()}
        a_files = {x: (src / x).read_text(encoding="utf-8") for x in FLAT_FILES}
        # 1. повтор на исходном эпизоде: те же записи, переходы и награды; reset не хранит прошлое
        t1, r1 = self.script(src)
        t2, r2 = self.script(src)
        self.assertEqual((t1, r1), (t2, r2))
        again = TMP / ("again%02d" % n)
        J.generate_episode(D_SEED0 + n, D_MATRIX[n][1], str(again))
        self.assertEqual({x: (again / x).read_text(encoding="utf-8") for x in FLAT_FILES}, a_files)
        # 2. oracle на преобразованном варианте подаёт те же выводы с новыми ID
        fa, fb = J.oracle_findings(str(src)), J.oracle_findings(str(dst))
        self.assertEqual(_canon_findings(fb, inv), _canon_findings(fa))
        if remap:
            used = {x for f in fb["findings"] for x in f["affected_ids"] + f["evidence_ids"]}
            self.assertFalse(used & set(m))  # старых ID в новом ответе нет (кроме fx_id)
        # 3. TP/FP/FN, P, R, F1 и награда совпадают при одинаковом inspection_steps
        for k in (0, 3):
            ra = J.run_on_episode(J.OraclePolicy(str(src), k), str(src))
            rb = J.run_on_episode(J.OraclePolicy(str(dst), k), str(dst))
            self.assertEqual(ra["score"], rb["score"])
            self.assertAlmostEqual(ra["score"]["return"], 1 - 0.002 * k, delta=1e-9)
            for key in ("tp", "fp", "fn", "precision", "recall", "f1", "reward", "inspection_steps"):
                self.assertEqual(ra["env_result"][key], rb["env_result"][key], key)
        if fa["findings"]:  # сломанный ответ (сумма +1 при τ = 0) остаётся сломанным и после преобразования
            broken = [dict(x["findings"][0], claimed_amount_minor=x["findings"][0]["claimed_amount_minor"] + 1)
                      for x in (fa, fb)]
            sa = J.score_episode(str(src), {"episode_id": fa["episode_id"], "findings": broken[:1]}, 3)
            sb = J.score_episode(str(dst), {"episode_id": fb["episode_id"], "findings": broken[1:]}, 3)
            self.assertEqual(sa, sb)
            self.assertEqual(sb["tp"], 0)
        # 4. нового канала утечки или недоступного доказательства нет; независимая проверка данных проходит
        self.accessible(dst, fb)
        self.assertEqual(check_records(*load_flat_episode(dst)), [])
        pa, pb = load_public_dir(src), load_public_dir(dst)
        self.assertEqual(sorted(r["fx_id"] for r in pa["fx"]), sorted(r["fx_id"] for r in pb["fx"]))
        if permute:
            self.assertNotEqual([r["row_id"] for r in pa["ledger"]], [inv.get(r["row_id"], r["row_id"]) for r in pb["ledger"]])
        else:
            self.assertEqual([r["row_id"] for r in pa["ledger"]], [inv[r["row_id"]] for r in pb["ledger"]])
        self.assertEqual(len(pa["ledger"]), len(pb["ledger"]))
        self.assertEqual(set(pb["ledger"][0]), set(LEDGER_COLS))

    def test_P01_P05_permutation(self):
        for n in range(0, 5):
            with self.subTest(pair="P%02d" % (n + 1)):
                self.pair(n, permute=True, remap=False)

    def test_P06_P10_id_remap(self):
        for n in range(5, 10):
            with self.subTest(pair="P%02d" % (n + 1)):
                self.pair(n, permute=False, remap=True)

    def test_P11_P20_permutation_and_remap(self):
        for n in range(10, 20):
            with self.subTest(pair="P%02d" % (n + 1)):
                self.pair(n, permute=True, remap=True)


if __name__ == "__main__":
    unittest.main()
