"""Схемы schemas/*.json совпадают с фактическим поведением кода.

Проверяющий здесь — маленький валидатор подмножества JSON Schema draft-04 (только стандартная
библиотека): integer — целое число без дробной части в записи, pattern — re.search Python."""
import json
import re
import unittest
from pathlib import Path

from auditgym.env import MAX_STEPS, AuditEnv
from auditgym.matcher import SubmissionError, validate_submission
from tests.helpers import ERRS, episode, oracle_findings

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


def load(name):
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


_TYPES = {
    "object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list), "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool), "null": lambda v: v is None,
}


def valid(inst, schema, root=None) -> bool:
    root = schema if root is None else root
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/"):
            target = root
            for part in ref[2:].split("/"):
                target = target[part]
            return valid(inst, target, root)
        other = load(ref)
        return valid(inst, other, other)
    t = schema.get("type")
    if t is not None and not any(_TYPES[x](inst) for x in ([t] if isinstance(t, str) else t)):
        return False
    if "enum" in schema and not any(type(inst) is type(e) and inst == e for e in schema["enum"]):
        return False
    if isinstance(inst, str):
        if len(inst) > schema.get("maxLength", len(inst)) or len(inst) < schema.get("minLength", 0):
            return False
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            return False
    if _TYPES["number"](inst):
        if inst < schema.get("minimum", inst) or inst > schema.get("maximum", inst):
            return False
    if isinstance(inst, list):
        if len(inst) > schema.get("maxItems", len(inst)) or len(inst) < schema.get("minItems", 0):
            return False
        if "items" in schema and not all(valid(x, schema["items"], root) for x in inst):
            return False
    if isinstance(inst, dict):
        if not set(schema.get("required", ())) <= set(inst):
            return False
        props = schema.get("properties", {})
        for k, v in inst.items():
            if k in props:
                if not valid(v, props[k], root):
                    return False
            elif schema.get("additionalProperties", True) is False:
                return False
            elif isinstance(schema.get("additionalProperties"), dict) and not valid(v, schema["additionalProperties"], root):
                return False
    if "allOf" in schema and not all(valid(inst, s, root) for s in schema["allOf"]):
        return False
    if "anyOf" in schema and not any(valid(inst, s, root) for s in schema["anyOf"]):
        return False
    if "oneOf" in schema and sum(valid(inst, s, root) for s in schema["oneOf"]) != 1:
        return False
    if "not" in schema and valid(inst, schema["not"], root):
        return False
    return True


class ValidatorSelfTest(unittest.TestCase):
    def test_draft04_integer_and_refs(self):
        self.assertTrue(valid(50, {"type": "integer"}))
        self.assertFalse(valid(50.0, {"type": "integer"}))
        self.assertFalse(valid(True, {"type": "integer"}))
        self.assertFalse(valid({"a": 1}, {"type": "object", "additionalProperties": False}))
        self.assertTrue(valid(1, {"oneOf": [{"type": "string"}, {"type": "integer"}]}))


def finding(**kw):
    f = {"type": "fx_rate", "affected_ids": ["INV-1"], "claimed_amount_minor": 100, "currency": "USD",
         "evidence_ids": ["DOC-1"], "explanation": ""}
    f.update(kw)
    return f


class FindingsSchema(unittest.TestCase):
    """Схема ответа принимает ровно то, что принимает matcher.validate_submission."""

    def test_schema_matches_validate_submission(self):
        schema = load("findings.schema.json")
        cases = [finding(), finding(currency=" usd "), finding(currency="uSd"), finding(currency="usd\n"),
                 finding(currency="ıss"), finding(currency="ﬃ"), finding(currency="us"), finding(currency="usd1"),
                 finding(currency="U S"), finding(currency=5), finding(evidence_ids=[" "]), finding(evidence_ids=[""]),
                 finding(evidence_ids=[]), finding(evidence_ids=["x" * 64]), finding(evidence_ids=["x" * 65]),
                 finding(evidence_ids=[" a "]), finding(evidence_ids=["e"] * 11), finding(affected_ids=[]),
                 finding(affected_ids=["\t"]), finding(affected_ids=["a"] * 10), finding(affected_ids=["a"] * 11),
                 finding(affected_ids="INV-1"), finding(claimed_amount_minor=100.0), finding(claimed_amount_minor=True),
                 finding(claimed_amount_minor=10 ** 15), finding(claimed_amount_minor=-10 ** 15 - 1),
                 finding(claimed_amount_minor="100"), finding(type="other"), finding(explanation="я" * 1000),
                 finding(explanation="я" * 1001), finding(explanation=None), dict(finding(), extra=1)]
        for f in cases:
            for payload in ({"episode_id": "EP-1", "findings": [f]}, ):
                try:
                    validate_submission(payload, "EP-1")
                    code_ok = True
                except SubmissionError:
                    code_ok = False
                self.assertEqual(valid(payload, schema), code_ok, repr(f)[:200])
        for payload in ({"episode_id": "EP-1", "findings": []}, {"episode_id": "EP-1", "findings": [finding()] * 30},
                        {"episode_id": "EP-1", "findings": [finding()] * 31}, {"episode_id": "EP-1"},
                        {"episode_id": "EP-1", "findings": [], "x": 1}, {"episode_id": 1, "findings": []}, []):
            try:
                validate_submission(payload, "EP-1")
                code_ok = True
            except SubmissionError:
                code_ok = False
            self.assertEqual(valid(payload, schema), code_ok, repr(payload)[:200])


class ActionSchema(unittest.TestCase):
    """Действия без зависимости от данных: схема принимает действие ⇔ среда отвечает без ошибки."""

    def test_schema_matches_env(self):
        schema = load("action.schema.json")
        ep = episode(5, ERRS)
        row, inv = ep.public["ledger"][0]["row_id"], ep.public["invoices"][0]
        cases = [
            {"type": "list_records", "table": "ledger"}, {"type": "list_records", "table": "ledger", "limit": 50},
            {"type": "list_records", "table": "ledger", "limit": 50.0}, {"type": "list_records", "table": "ledger", "limit": 1000},
            {"type": "list_records", "table": "ledger", "limit": 0}, {"type": "list_records", "table": "ledger", "limit": True},
            {"type": "list_records", "table": "ledger", "limit": "5"}, {"type": "list_records", "table": "ledger", "cursor": None},
            {"type": "list_records", "table": "ledger", "cursor": "ledger:50"}, {"type": "list_records", "table": "ledger", "cursor": "ledger: 5"},
            {"type": "list_records", "table": "ledger", "cursor": "fx:0"}, {"type": "list_records", "table": "fx", "cursor": "fx:0"},
            {"type": "list_records", "table": "ledger", "cursor": "ledger:x"}, {"type": "list_records", "table": "ledger", "cursor": 5},
            {"type": "list_records", "table": "secret"}, {"type": "list_records"}, {"type": "list_records", "table": "ledger", "sql": 1},
            {"type": "get_record", "table": "ledger", "id": row}, {"type": "get_record", "table": "invoices", "id": inv["doc_id"]},
            {"type": "get_record", "table": "ledger"}, {"type": "get_record", "table": "nope", "id": row},
            {"type": "get_record", "table": "ledger", "id": 5}, {"type": "get_document", "doc_id": inv["doc_id"]},
            {"type": "get_document"}, {"type": "get_document", "doc_id": 1}, {"type": "calculator", "expression": "(1+2)*3/4"},
            {"type": "calculator", "expression": "7 // 2 % 3"}, {"type": "calculator", "expression": "2**3"},
            {"type": "calculator", "expression": "a+1"}, {"type": "calculator", "expression": "1e5"},
            {"type": "calculator", "expression": "1" * 301}, {"type": "calculator", "expression": "()"},
            {"type": "calculator", "expression": ""}, {"type": "calculator", "expression": 5}, {"type": "calculator"},
            {"type": "shell", "cmd": "ls"}, {"type": 5}, {}, [], "text", None,
        ]
        for a in cases:
            env = AuditEnv()
            env.reset(5, ERRS)
            obs = env.step(a)[0]
            self.assertEqual(valid(a, schema), obs["error"] is None, "%r → %r" % (a, obs["error"]))

    def test_submit_schema_matches_grader(self):
        schema = load("action.schema.json")
        ep = episode(5, ERRS)
        good = {"episode_id": ep.episode_id, "findings": oracle_findings(ep)}
        for a in ({"type": "submit", "findings": good}, {"type": "submit", "findings": dict(good, extra=1)},
                  {"type": "submit", "findings": good, "extra": 1}, {"type": "submit"}, {"type": "submit", "findings": None}):
            env = AuditEnv()
            env.reset(5, ERRS)
            env.step(a)
            self.assertEqual(valid(a, schema), env.grader_report()["result"]["valid"], repr(a)[:120])


class ObservationSchema(unittest.TestCase):
    def test_observations_and_info_match_schema(self):
        schema = load("observation.schema.json")
        defs = schema["definitions"]
        ep = episode(5, ERRS)
        for mode in ("train", "eval"):
            env = AuditEnv(mode=mode)
            obs, info = env.reset(5, ERRS)
            self.assertTrue(valid(obs, schema), "reset")
            self.assertTrue(valid(info, defs["reset_info"], schema))
            acts = [{"type": "list_records", "table": t, "limit": 50} for t in ("ledger", "invoices", "payments", "fx")]
            acts += [{"type": "get_record", "table": "payments", "id": ep.public["payments"][0]["payment_id"]},
                     {"type": "get_document", "doc_id": ep.public["invoices"][0]["doc_id"]},
                     {"type": "get_document", "doc_id": ep.public["payments"][0]["doc_id"]},
                     {"type": "calculator", "expression": "1/3"}, {"type": "get_record", "table": "ledger", "id": "L-NOPE"},
                     {"type": "nope"}, {"type": "list_records", "table": "ledger", "cursor": "ledger:x"},
                     {"type": "calculator", "expression": "1/0"}, {"type": "list_records", "table": "ledger", "z" * 500: 1},
                     {"type": "submit", "findings": {"episode_id": ep.episode_id, "findings": oracle_findings(ep)}},
                     {"type": "calculator", "expression": "1"}]
            for a in acts:
                o, r, te, tr, inf = env.step(a)
                self.assertTrue(valid(o, schema), "%r → %r" % (a, o))
                self.assertTrue(valid(inf, defs["step_info"], schema), inf)
        now = [0.0]
        env = AuditEnv(clock=lambda: now[0])
        env.reset(5, {})
        now[0] = 61.0
        o = env.step({"type": "calculator", "expression": "1"})[0]
        self.assertTrue(valid(o, schema), o)
        env = AuditEnv()
        env.reset(5, {})
        for _ in range(MAX_STEPS):
            o = env.step({"type": "calculator", "expression": "1"})[0]
            self.assertTrue(valid(o, schema), o)


if __name__ == "__main__":
    unittest.main()
