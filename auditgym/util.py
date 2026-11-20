"""Общие детерминированные утилиты: хэши, генераторы случайных чисел, канонический JSON.

Никаких hash(), системного времени и порядка обхода множеств на пути к выходу: одинаковые
seed, конфиг и версия обязаны давать побайтно одинаковые эпизоды на любой машине.
"""
from __future__ import annotations

import base64
import hashlib
import json
import random


def h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def rng(*parts) -> random.Random:
    return random.Random(int(h(*parts)[:16], 16))


def b32(hex_digest: str, n: int) -> str:
    return base64.b32encode(bytes.fromhex(hex_digest)).decode("ascii").rstrip("=")[:n]


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
