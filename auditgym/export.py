"""Запись и чтение эпизодов в форматах раздела 03 ТЗ (UTF-8 везде).

DIR/public/<episode_id>/   manifest.json ledger.csv invoices.jsonl payments.jsonl fx.csv policy.json
DIR/grader/<episode_id>/   truth.json provenance.json pre_injection/{ledger.csv,invoices.jsonl,payments.jsonl}
Каталог grader/ — только для оценщика; агенту он никогда не доступен.
"""
from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path

from .generator import generate
from .money import fx_id

LEDGER_COLS = ["row_id", "timestamp", "account", "debit_minor", "credit_minor", "currency", "entity_id",
               "invoice_id", "payment_id"]
FX_COLS = ["date", "source_currency", "target_currency", "rate"]
NULL = "null"
ENC = "utf-8"


def _csv(rows, cols) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([NULL if r[c] is None else r[c] for c in cols])
    return buf.getvalue()


def _jsonl(rows) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def public_files(public: dict) -> dict[str, str]:
    return {
        "manifest.json": json.dumps(public["manifest"], indent=2, ensure_ascii=False) + "\n",
        "policy.json": json.dumps(public["policy"], indent=2, ensure_ascii=False) + "\n",
        "ledger.csv": _csv(public["ledger"], LEDGER_COLS),
        "invoices.jsonl": _jsonl(public["invoices"]),
        "payments.jsonl": _jsonl(public["payments"]),
        "fx.csv": _csv(public["fx"], FX_COLS),
    }


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding=ENC)


def write_episode(ep, root: Path) -> tuple[Path, Path]:
    pdir, gdir = root / "public" / ep.episode_id, root / "grader" / ep.episode_id
    pdir.mkdir(parents=True, exist_ok=True)
    (gdir / "pre_injection").mkdir(parents=True, exist_ok=True)
    for name, text in public_files(ep.public).items():
        (pdir / name).write_text(text, encoding=ENC)
    write_json(gdir / "truth.json", ep.truth)
    write_json(gdir / "provenance.json", ep.provenance)
    for name, text in public_files(ep.clean_public).items():
        if name in ("ledger.csv", "invoices.jsonl", "payments.jsonl"):
            (gdir / "pre_injection" / name).write_text(text, encoding=ENC)
    return pdir, gdir


def export_split(entries, split: str, out_dir: Path) -> list[dict]:
    target = out_dir / split
    if target.exists():  # эпизоды прошлых версий не должны оставаться рядом
        shutil.rmtree(target)
    index = []
    for e in entries:
        ep = generate(e["seed"], e["config"])
        write_episode(ep, target)
        index.append({"name": e["name"], "seed": e["seed"], "episode_id": ep.episode_id, "digest": ep.digest(),
                      "n_issues": len(ep.truth["issues"]), "counts": ep.public["manifest"]["counts"]})
    write_json(target / "index.json", index)
    return index


def _read_ledger(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding=ENC, newline="") as f:
        for r in csv.DictReader(f):
            for k, v in r.items():
                if v == NULL:
                    r[k] = None
                elif k in ("debit_minor", "credit_minor"):
                    r[k] = int(v)
            rows.append(r)
    return rows


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding=ENC).splitlines() if l.strip()]


def load_public_dir(pdir: Path) -> dict:
    fx = []
    with (pdir / "fx.csv").open(encoding=ENC, newline="") as f:
        for r in csv.DictReader(f):
            r["fx_id"] = fx_id(r["date"], r["source_currency"], r["target_currency"])
            fx.append({"fx_id": r["fx_id"], "date": r["date"], "source_currency": r["source_currency"],
                       "target_currency": r["target_currency"], "rate": r["rate"]})
    return {
        "manifest": json.loads((pdir / "manifest.json").read_text(encoding=ENC)),
        "policy": json.loads((pdir / "policy.json").read_text(encoding=ENC)),
        "ledger": _read_ledger(pdir / "ledger.csv"),
        "invoices": _read_jsonl(pdir / "invoices.jsonl"),
        "payments": _read_jsonl(pdir / "payments.jsonl"),
        "fx": fx,
    }


def load_episode_files(root: Path, episode_id: str):
    """→ (public, pre_injection, truth, provenance), только из файлов."""
    pdir, gdir = root / "public" / episode_id, root / "grader" / episode_id
    public = load_public_dir(pdir)
    clean = dict(public)
    clean["ledger"] = _read_ledger(gdir / "pre_injection" / "ledger.csv")
    clean["invoices"] = _read_jsonl(gdir / "pre_injection" / "invoices.jsonl")
    clean["payments"] = _read_jsonl(gdir / "pre_injection" / "payments.jsonl")
    clean["manifest"] = dict(public["manifest"], counts={"ledger": len(clean["ledger"])})
    truth = json.loads((gdir / "truth.json").read_text(encoding=ENC))
    prov = json.loads((gdir / "provenance.json").read_text(encoding=ENC))
    return public, clean, truth, prov
