"""Безвредное преобразование экспортированного эпизода прямо в файлах: каждый ID записи
(row_id, doc_id, invoice_id, payment_id, entity_id, episode_id) согласованно переименовывается
во всех открытых файлах, эталоне, provenance и книгах до внесения; каждая таблица
перемешивается. ID курсов выводятся из (даты, валют) и не меняются.

Среда прогоняет такой эпизод через
``reset(seed, {"episode_files": {"root": DIR, "episode_id": ID}})``; парные проверки
показывают, что ответ oracle и награда сохраняются с учётом новых ID.
"""
from __future__ import annotations

import base64
import hashlib
import random
import shutil
from pathlib import Path

from .export import ENC, load_episode_files, public_files, write_json

ID_FIELDS = ("row_id", "doc_id", "invoice_id", "payment_id", "entity_id")


def _new_id(old: str, salt: str) -> str:
    prefix = old.split("-", 1)[0] + "-"
    digest = hashlib.sha256(("%s|%s" % (salt, old)).encode("utf-8")).digest()
    return prefix + "X" + base64.b32encode(digest).decode("ascii")[: len(old) - len(prefix) + 1]


def _rename(obj, m):
    if isinstance(obj, str):
        return m.get(obj, obj)
    if isinstance(obj, list):
        return [_rename(x, m) for x in obj]
    if isinstance(obj, dict):
        return {(m.get(k, k) if isinstance(k, str) else k): _rename(v, m) for k, v in obj.items()}
    return obj


def transform_episode(src_root: Path, episode_id: str, dst_root: Path, salt: str = "t1", order_seed: int = 1) -> str:
    public, clean, truth, prov = load_episode_files(src_root, episode_id)
    ids = {public["manifest"]["episode_id"]}
    for book in (public, clean):
        for t in ("ledger", "invoices", "payments"):
            for r in book[t]:
                ids.update(r[f] for f in ID_FIELDS if r.get(f))
                ids.update(x["invoice_id"] for x in r.get("invoice_refs") or [])
    m = {i: _new_id(i, salt) for i in sorted(ids)}
    if len(set(m.values())) != len(m):
        raise RuntimeError("коллизия ID при преобразовании")
    order = random.Random("%s|%d" % (salt, order_seed))
    pub2, clean2 = _rename(public, m), _rename(clean, m)
    for book in (pub2, clean2):
        for t in ("ledger", "invoices", "payments", "fx"):
            order.shuffle(book[t])
    new_eid = m[episode_id]
    pdir, gdir = dst_root / "public" / new_eid, dst_root / "grader" / new_eid
    for d in (pdir, gdir / "pre_injection"):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    for name, text in public_files(pub2).items():
        (pdir / name).write_text(text, encoding=ENC)
    for name, text in public_files(clean2).items():
        if name in ("ledger.csv", "invoices.jsonl", "payments.jsonl"):
            (gdir / "pre_injection" / name).write_text(text, encoding=ENC)
    write_json(gdir / "truth.json", _rename(truth, m))
    prov2 = _rename(prov, m)
    prov2["transformation"] = {"from_episode_id": episode_id, "salt": salt, "order_seed": order_seed, "id_map": m}
    write_json(gdir / "provenance.json", prov2)
    return new_eid
