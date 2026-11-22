"""Общие помощники тестов."""
import copy

from auditgym.generator import generate
from auditgym.matcher import match_context, score

ERRS = {"errors": {"duplicate_invoice": 1, "payment_allocation": 1, "fx_rate": 1, "period_cutoff": 1}}
CLEAN = {}
_cache = {}


def episode(seed=11, cfg=None):
    key = (seed, repr(cfg))
    if key not in _cache:
        _cache[key] = generate(seed, copy.deepcopy(cfg if cfg is not None else ERRS))
    return _cache[key]


def oracle_findings(ep):
    return [{"type": i["type"], "affected_ids": list(i["affected_ids"]), "claimed_amount_minor": i["expected_amount_minor"],
             "currency": i["currency"], "evidence_ids": list(i["evidence_sets"][0]), "explanation": "x"}
            for i in ep.truth["issues"]]


def grade(ep, findings, steps=0):
    return score({"episode_id": ep.episode_id, "findings": findings}, ep.truth, match_context(ep.public), steps)


def submit(env, ep, findings):
    return env.step({"type": "submit", "findings": {"episode_id": ep.episode_id, "findings": findings}})
