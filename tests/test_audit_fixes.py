"""Сценарии исправлений по аудиту (selfai-request.md, часть 4, сценарии 1–6).

1. Каждый `team_config` из таблицы JURY.md «D01–D20 конфигом генератора» принимается
   `judge_adapter.generate_episode` как есть.
2. Тот же JSON совпадает по значению с `D_MATRIX` из tests/test_judge_pairs.py (D05 — с
   `boundary_fx_partial: 2`).
3. Возврат, закрывающий счёт в ноль, проводит по 1200 ровно остаток счёта (разница — на 7800);
   правило записано в README и DESIGN.
4–5. `git_state()` без git: коммит из `AUDITGYM_GIT_COMMIT` или null, `dirty` — null.
6. В `.github/workflows/` лежат docker-check.yml и update-reports.yml; образ собирается с
   `--build-arg GIT_COMMIT` и запускается официальной командой.
"""
import csv
import json
import os
import re
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import judge_adapter as J
from auditgym import __main__ as M
from auditgym.export import FLAT_FILES
from auditgym.money import convert_minor, local_date
from tests.test_judge_pairs import D_MATRIX, D_SEED0

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
JURY = ROOT / "JURY.md"
DESIGN = ROOT / "DESIGN.md"
WORKFLOWS = ROOT / ".github" / "workflows"


def readme_d_table() -> list[tuple[str, str]]:
    """Строки таблицы «D01–D20 конфигом генератора»: (метка эпизода, текст ячейки team_config)."""
    lines = JURY.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("###") and "D01–D20 конфигом генератора" in l)
    rows = []
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        s = line.strip()
        if not s.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells or not re.match(r"^D\d", cells[0]):
            continue  # заголовок и разделитель
        rows.append((cells[0], cells[-1]))
    return rows


def cell_json(cell: str):
    """JSON внутри ячейки: ровно одна вставка `...` с объектом."""
    parts = re.findall(r"`([^`]*)`", cell)
    if len(parts) != 1:
        raise AssertionError("в ячейке team_config ожидается ровно один `JSON`, а там: %r" % cell)
    return json.loads(parts[0])


class ReadmeDConfigs(unittest.TestCase):
    """Сценарии 1 и 2."""

    def test_readme_has_20_rows_D01_D20(self):
        labels = [l for l, _ in readme_d_table()]
        self.assertEqual(labels, ["D%02d" % n for n in range(1, 21)])

    def test_readme_team_configs_generate(self):
        rows = readme_d_table()
        self.assertEqual(len(rows), 20, [l for l, _ in rows])
        tmp = Path(tempfile.mkdtemp(prefix="auditgym-readme-d-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for n, (label, cell) in enumerate(rows):
            with self.subTest(label):
                cfg = cell_json(cell)
                out = tmp / label
                J.generate_episode(D_SEED0 + n, cfg, str(out))
                self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(FLAT_FILES))

    def test_readme_team_configs_equal_D_MATRIX(self):
        rows = readme_d_table()
        self.assertEqual(len(rows), len(D_MATRIX))
        for n, (label, cell) in enumerate(rows):
            with self.subTest(label):
                self.assertEqual(cell_json(cell), D_MATRIX[n][1])
        d05 = cell_json(rows[4][1])
        self.assertEqual(d05["distractors"]["boundary_fx_partial"], 2)


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _net(rows) -> int:
    return sum(int(r["debit_minor"]) - int(r["credit_minor"]) for r in rows)


def closing_refunds(ep: Path) -> list[dict]:
    """Возвраты эпизода, проведённые в книге, с суммами по файлам эпизода."""
    man = json.loads((ep / "manifest.json").read_text(encoding="utf-8"))
    base, tz = man["base_currency"], man["timezone"]
    ledger = _csv(ep / "ledger.csv")
    invoices = {i["invoice_id"]: i for i in _jsonl(ep / "invoices.jsonl")}
    fx = {(r["date"], r["source_currency"], r["target_currency"]): Decimal(r["rate"]) for r in _csv(ep / "fx.csv")}
    out = []
    for p in _jsonl(ep / "payments.jsonl"):
        if p["amount_minor"] >= 0 or not p["invoice_refs"] or len(p["invoice_refs"]) != 1:
            continue
        own = [r for r in ledger if r["payment_id"] == p["payment_id"]]
        if not own:
            continue  # вне периода, не проведён
        ref = p["invoice_refs"][0]
        inv = invoices[ref["invoice_id"]]
        ar = [r for r in ledger if r["account"].startswith("1200") and r["invoice_id"] == inv["invoice_id"]]
        rate = Decimal(1) if inv["currency"] == base else \
            fx[(local_date(inv["issued_at"], tz).isoformat(), inv["currency"], base)]
        out.append({
            "payment": p, "invoice": inv, "rows": own,
            "balance_before": _net(r for r in ar if r["payment_id"] != p["payment_id"]),
            "balance_after": _net(ar),
            "refund_1200": _net(r for r in own if r["account"].startswith("1200")),
            "cash_1000": _net(r for r in own if r["account"].startswith("1000")),
            "fx_7800": _net(r for r in own if r["account"].startswith("7800")),
            "naive_1200": convert_minor(-ref["amount_minor"], inv["currency"], base, rate),
        })
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("`", "").replace("*", "")).lower()


class RefundClosingRule(unittest.TestCase):
    """Сценарий 3: возврат, закрывающий счёт в ноль, списывает остаток 1200 целиком."""

    def test_refund_posts_remaining_balance(self):
        cfg = dict(D_MATRIX[2][1])  # D03: {"distractors": {"refund": 2, остальное 0}}
        self.assertEqual(cfg["distractors"]["refund"], 2)
        tmp = Path(tempfile.mkdtemp(prefix="auditgym-refund-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        checked = rounding_cases = 0
        for seed in range(D_SEED0, D_SEED0 + 80):
            ep = tmp / str(seed)
            J.generate_episode(seed, cfg, str(ep))
            for c in closing_refunds(ep):
                pid = c["payment"]["payment_id"]
                with self.subTest(seed=seed, payment=pid):
                    checked += 1
                    self.assertLess(c["balance_before"], 0, "до возврата — кредитовое сальдо 1200 (переплата)")
                    self.assertEqual(c["balance_after"], 0, "возврат закрывает счёт в ноль")
                    self.assertEqual(c["refund_1200"], -c["balance_before"], "строка 1200 возврата = остаток счёта")
                    self.assertEqual(c["fx_7800"], -(c["cash_1000"] + c["refund_1200"]), "разница — на 7800")
                    self.assertEqual(_net(c["rows"]), 0, "проводка сбалансирована")
                    if c["naive_1200"] != c["refund_1200"]:
                        rounding_cases += 1
            if rounding_cases >= 2:
                break
        self.assertGreater(checked, 0, "не найдено ни одного проведённого возврата")
        self.assertGreater(rounding_cases, 0,
                           "не найден возврат, где округление суммы возврата по курсу счёта даёт расхождение")

    def test_rule_documented_in_readme_and_design(self):
        for path in (README, DESIGN):
            with self.subTest(path.name):
                text = _norm(path.read_text(encoding="utf-8"))
                ok = False
                for m in re.finditer("возврат", text):
                    w = text[max(0, m.start() - 400): m.start() + 500]
                    if all(k in w for k in ("в ноль", "остаток 1200", "7800", "переплат")):
                        ok = True
                        break
                self.assertTrue(ok, "%s: нет фразы о том, что возврат, закрывающий счёт в ноль, списывает "
                                    "остаток 1200 целиком, разница — на 7800, а переплата законна" % path.name)


class GitStateWithoutGit(unittest.TestCase):
    """Сценарии 4 и 5: git_state() без git (каталог без .git; git нет в PATH)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="auditgym-nogit-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "reports").mkdir()

    def _conditions(self):
        """Два честных способа остаться без git: ROOT без .git (поиск .git выше запрещён) и пустой PATH."""
        ceiling = {"GIT_CEILING_DIRECTORIES": str(self.tmp.parent)}
        yield "root-without-.git", ceiling
        yield "no-git-in-PATH", dict(ceiling, PATH=str(self.tmp))

    def _run(self, env_extra, commit_var):
        env = {k: v for k, v in os.environ.items() if k != "AUDITGYM_GIT_COMMIT"}
        env.update(env_extra)
        if commit_var is not None:
            env["AUDITGYM_GIT_COMMIT"] = commit_var
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(M, "ROOT", self.tmp):
            return M.git_state()

    def test_commit_from_env_variable(self):
        for name, env in self._conditions():
            with self.subTest(name):
                self.assertEqual(self._run(env, "abc123"), {"commit": "abc123", "dirty": None})

    def test_no_git_no_variable_is_null(self):
        for name, env in self._conditions():
            with self.subTest(name):
                self.assertEqual(self._run(env, None), {"commit": None, "dirty": None})


class Workflows(unittest.TestCase):
    """Сценарий 6: workflow, которыми собраны отчёты, лежат в .github/workflows."""

    def _read(self, name):
        p = WORKFLOWS / name
        self.assertTrue(p.is_file(), "нет %s" % p.relative_to(ROOT))
        return p.read_text(encoding="utf-8")

    @staticmethod
    def _run_lines(text):
        return [m.group(1).strip() for m in re.finditer(r"^\s*(?:-\s*)?run:\s*(.+)$", text, re.M)]

    def test_docker_check(self):
        text = self._read("docker-check.yml")
        runs = self._run_lines(text)
        build = [r for r in runs if r.startswith("docker build")]
        self.assertEqual(len(build), 1, runs)
        self.assertRegex(build[0], r"--build-arg\s+GIT_COMMIT=\$\{\{\s*github\.sha\s*\}\}")
        self.assertRegex(build[0], r"-t\s+auditgym\s+\.\s*$")
        self.assertIn("docker run --rm --network none --cpus 2 --memory 4g auditgym", runs)
        self.assertRegex(text, r"(?m)^on:\s*$")
        self.assertRegex(text, r"(?m)^\s+branches:\s*\[ci-docker-check\]\s*$")
        self.assertRegex(text, r"(?m)^\s+workflow_dispatch:")

    def test_update_reports(self):
        text = self._read("update-reports.yml")
        self.assertRegex(text, r"(?m)^on:\s*$")
        self.assertRegex(text, r"(?m)^\s+branches:\s*\[update-reports\]\s*$")
        self.assertRegex(text, r"(?m)^\s+workflow_dispatch:")
        self.assertTrue(any("python -m auditgym check" in r for r in self._run_lines(text)))
        self.assertIn("git push origin HEAD:main", text)

    def test_no_isolation_attack(self):
        self.assertFalse((WORKFLOWS / "isolation-attack.yml").exists())


if __name__ == "__main__":
    unittest.main()
