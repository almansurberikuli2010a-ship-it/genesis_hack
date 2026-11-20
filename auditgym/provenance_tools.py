"""Обращение записанного внесения: книги ДО ошибок из открытых книг и provenance.json.
Ни генератор, ни снимок pre_injection/ для этого не нужны."""
from __future__ import annotations

import json


def reconstruct_pre_injection(public: dict, provenance: dict) -> dict:
    diff = provenance["ledger_diff"]
    added = {x["row"]["row_id"] for x in diff["added"]}
    ledger = [r for r in public["ledger"] if r["row_id"] not in added] + [x["row"] for x in diff["removed"]]
    docs_added = {d["doc_id"] for d in provenance["documents_added"]}
    out = dict(public)
    out["ledger"] = ledger
    out["invoices"] = [i for i in public["invoices"] if i["doc_id"] not in docs_added]
    out["payments"] = [p for p in public["payments"] if p["doc_id"] not in docs_added]
    out["manifest"] = dict(public["manifest"], counts={"ledger": len(ledger)})
    return out


def _canon(rows) -> list:
    return sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows)


def same_books(a: dict, b: dict) -> list[str]:
    """Сравнение книги, счетов и платежей без учёта порядка."""
    return ["%s различаются" % t for t in ("ledger", "invoices", "payments") if _canon(a[t]) != _canon(b[t])]


def unattributed_changes(provenance: dict) -> list[str]:
    return ["%s: строка %s без записанной причины" % (side, x["row"]["row_id"])
            for side in ("added", "removed") for x in provenance["ledger_diff"][side] if not x["caused_by"]]
