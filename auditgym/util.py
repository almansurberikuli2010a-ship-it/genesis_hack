"""Общие детерминированные утилиты: хэши, генераторы случайных чисел, канонический JSON.

Никаких hash(), системного времени и порядка обхода множеств на пути к выходу: одинаковые
seed, конфиг и версия обязаны давать побайтно одинаковые эпизоды на любой машине.
hashlib.scrypt есть в любой сборке Python ≥ 3.10 (нужен OpenSSL ≥ 1.1.1, PEP 644).
"""
from __future__ import annotations

import base64
import functools
import hashlib
import json
import random

# Параметры медленной KDF ключа истории (scrypt, RFC 7914): 8 МиБ памяти и ~15 мс (Apple M1) на вызов.
# Они часть определения эпизода: смена параметров меняет все эпизоды.
SLOW_KDF = {"n": 2 ** 13, "r": 8, "p": 1}
_SLOW_SALT = b"auditgym/story-key/v1"


def h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=4096)
def _slow_hash(text: str) -> str:
    return hashlib.scrypt(text.encode("utf-8"), salt=_SLOW_SALT, dklen=32, maxmem=64 * 2 ** 20, **SLOW_KDF).hex()


def slow_h(*parts) -> str:
    """Как ``h``, но через медленную память-ёмкую KDF: перебор seed по открытому наблюдению
    (episode_id, название компании) стоит ~15 мс и 8 МиБ на кандидата вместо микросекунд. Результат
    детерминирован; повторный вызов с теми же частями в том же процессе берётся из кэша."""
    return _slow_hash("|".join(str(p) for p in parts))


def rng(*parts) -> random.Random:
    return random.Random(int(h(*parts)[:16], 16))


def b32(hex_digest: str, n: int) -> str:
    return base64.b32encode(bytes.fromhex(hex_digest)).decode("ascii").rstrip("=")[:n]


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
