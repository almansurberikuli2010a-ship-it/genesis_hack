"""Общий matcher и терминальная награда (раздел 05 ТЗ). Код открыт и опубликован.

Нормализация
------------
* ``type`` — точная строка из перечня (иначе — нарушение схемы);
* ``affected_ids`` — обрезка пробелов, сравнение как МНОЖЕСТВ; doc_id принимается как синоним
  invoice_id / payment_id;
* ``currency`` — обрезка пробелов, верхний регистр;
* ``evidence_ids`` — множество; invoice_id / payment_id принимаются как синонимы doc_id.

Совместимость (вывод f может быть TP для ошибки эталона t)
---------------------------------------------------------
тип совпадает И множество affected_ids совпадает И валюта совпадает И
|claimed − expected| ≤ допуск = max(1, floor(|expected| × 0.0005)) И
хотя бы один допустимый набор доказательств целиком входит в evidence_ids вывода.

Неоднозначность
---------------
Взаимно однозначное максимальное паросочетание (алгоритм Куна). Выводы обрабатываются в
порядке отправки, ошибки эталона — по возрастанию issue_id: ничьи разрешаются
детерминированно, повторный вывод не может занять вторую ошибку. Несопоставленные выводы —
FP, несопоставленные ошибки — FN.

Метрики и награда
-----------------
P = TP/(TP+FP), R = TP/(TP+FN), F1 = 2TP/(2TP+FP+FN); при нулевом знаменателе P или R = 0;
эталон и ответ пусты → F1 = 1; эталон пуст, ответ нет → F1 = 0.
Награда = clip(2·F1 − 1 − 0.002·inspection_steps, −1, 1).
"""
from __future__ import annotations

import re

FINDING_TYPES = ("duplicate_invoice", "payment_allocation", "fx_rate", "period_cutoff")
FINDING_KEYS = {"type", "affected_ids", "claimed_amount_minor", "currency", "evidence_ids", "explanation"}
MAX_FINDINGS = 30
MAX_EVIDENCE = 10
MAX_AFFECTED = 10
MAX_EXPLANATION = 1000
MAX_ID_LEN = 64
STEP_COST = 0.002
_CUR = re.compile(r"^[A-Z]{3}$")


class SubmissionError(ValueError):
    """Нарушение структуры findings.json."""


def amount_tolerance(expected: int) -> int:
    """Опубликованный допуск по сумме: max(1, floor(|expected| × 0.0005))."""
    return max(1, abs(int(expected)) * 5 // 10000)


def _id_list(v, name, max_len, allow_empty):
    if not isinstance(v, list):
        raise SubmissionError("%s должен быть списком" % name)
    if len(v) > max_len or (not allow_empty and not v):
        raise SubmissionError("%s: от %d до %d элементов" % (name, 0 if allow_empty else 1, max_len))
    for x in v:
        if not isinstance(x, str) or not x.strip() or len(x) > MAX_ID_LEN:
            raise SubmissionError("%s: элементы — непустые строки до %d символов" % (name, MAX_ID_LEN))
    return [x.strip() for x in v]


def validate_submission(obj, episode_id: str) -> list[dict]:
    """Выбросить SubmissionError при любой структурной ошибке; вернуть список выводов."""
    if not isinstance(obj, dict):
        raise SubmissionError("findings должен быть объектом")
    if set(obj) != {"episode_id", "findings"}:
        raise SubmissionError("ключи должны быть ровно episode_id и findings")
    if obj["episode_id"] != episode_id:
        raise SubmissionError("episode_id не совпадает")
    fs = obj["findings"]
    if not isinstance(fs, list):
        raise SubmissionError("findings.findings должен быть списком")
    if len(fs) > MAX_FINDINGS:
        raise SubmissionError("не больше %d выводов" % MAX_FINDINGS)
    out = []
    for i, f in enumerate(fs):
        if not isinstance(f, dict) or set(f) != FINDING_KEYS:
            raise SubmissionError("вывод %d: ключи должны быть ровно %s" % (i, sorted(FINDING_KEYS)))
        if f["type"] not in FINDING_TYPES:
            raise SubmissionError("вывод %d: неизвестный тип" % i)
        amt = f["claimed_amount_minor"]
        if not isinstance(amt, int) or isinstance(amt, bool) or abs(amt) > 10 ** 15:
            raise SubmissionError("вывод %d: claimed_amount_minor должен быть целым" % i)
        if not isinstance(f["currency"], str) or not _CUR.match(f["currency"].strip().upper()):
            raise SubmissionError("вывод %d: currency — трёхбуквенный код" % i)
        if not isinstance(f["explanation"], str) or len(f["explanation"]) > MAX_EXPLANATION:
            raise SubmissionError("вывод %d: explanation — строка до %d символов" % (i, MAX_EXPLANATION))
        out.append({
            "type": f["type"],
            "affected_ids": _id_list(f["affected_ids"], "affected_ids", MAX_AFFECTED, False),
            "claimed_amount_minor": amt,
            "currency": f["currency"].strip().upper(),
            "evidence_ids": _id_list(f["evidence_ids"], "evidence_ids", MAX_EVIDENCE, True),
            "explanation": f["explanation"],
        })
    return out


def build_aliases(public: dict) -> dict:
    """Синонимы doc_id ⇄ invoice_id/payment_id из открытых записей."""
    to_doc, to_biz = {}, {}
    for i in public["invoices"]:
        to_doc[i["invoice_id"]] = i["doc_id"]
        to_biz[i["doc_id"]] = i["invoice_id"]
    for p in public["payments"]:
        to_doc[p["payment_id"]] = p["doc_id"]
        to_biz[p["doc_id"]] = p["payment_id"]
    return {"to_doc": to_doc, "to_biz": to_biz}


def _norm(f, aliases):
    return {
        "type": f["type"],
        "affected": frozenset(aliases["to_biz"].get(x, x) for x in f["affected_ids"]),
        "amount": f["claimed_amount_minor"],
        "currency": f["currency"],
        "evidence": frozenset(aliases["to_doc"].get(x, x) for x in f["evidence_ids"]),
    }


def compatible(nf: dict, issue: dict) -> bool:
    if nf["type"] != issue["type"]:
        return False
    if nf["affected"] != frozenset(issue["affected_ids"]):
        return False
    if nf["currency"] != issue["currency"]:
        return False
    if abs(nf["amount"] - issue["expected_amount_minor"]) > amount_tolerance(issue["expected_amount_minor"]):
        return False
    return any(set(es) <= nf["evidence"] for es in issue["evidence_sets"])


def match(findings: list[dict], issues: list[dict], aliases: dict) -> dict:
    issues = sorted(issues, key=lambda t: t["issue_id"])
    norm = [_norm(f, aliases) for f in findings]
    adj = [[j for j, t in enumerate(issues) if compatible(nf, t)] for nf in norm]
    owner = [-1] * len(issues)

    def try_assign(i, seen):
        for j in adj[i]:
            if seen[j]:
                continue
            seen[j] = True
            if owner[j] == -1 or try_assign(owner[j], seen):
                owner[j] = i
                return True
        return False

    for i in range(len(findings)):
        try_assign(i, [False] * len(issues))
    pairs = sorted((owner[j], issues[j]["issue_id"]) for j in range(len(issues)) if owner[j] != -1)
    matched = {i for i, _ in pairs}
    return {
        "tp": len(pairs),
        "fp": len(findings) - len(pairs),
        "fn": len(issues) - len(pairs),
        "pairs": pairs,
        "unmatched_findings": [i for i in range(len(findings)) if i not in matched],
        "missed_issues": [t["issue_id"] for j, t in enumerate(issues) if owner[j] == -1],
    }


def metrics(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 1.0 if tp + fp + fn == 0 else 2 * tp / (2 * tp + fp + fn)
    return {"precision": p, "recall": r, "f1": f1}


def terminal_reward(f1: float, inspection_steps: int) -> float:
    return max(-1.0, min(1.0, 2.0 * f1 - 1.0 - STEP_COST * inspection_steps))


def experimental_reward(findings, issues, aliases, inspection_steps) -> float:
    """ЭКСПЕРИМЕНТАЛЬНАЯ награда (НЕ общая): половина за вывод с верными типом и ID, но неверной
    суммой, валютой или доказательствами. Показывается отдельно и никогда не подменяет общую."""
    norm = [_norm(f, aliases) for f in findings]
    m = match(findings, issues, aliases)
    credit = float(m["tp"])
    used = {i for i, _ in m["pairs"]}
    open_issues = set(m["missed_issues"])
    for i, nf in enumerate(norm):
        if i in used:
            continue
        for t in issues:
            if t["issue_id"] in open_issues and nf["type"] == t["type"] and nf["affected"] == frozenset(t["affected_ids"]):
                credit += 0.5
                open_issues.discard(t["issue_id"])
                break
    n_f, n_t = len(findings), len(issues)
    f1 = 1.0 if n_f == n_t == 0 else (2 * credit / (n_f + n_t) if n_f + n_t else 0.0)
    return terminal_reward(f1, inspection_steps)


def score(payload, truth: dict, aliases: dict, inspection_steps: int) -> dict:
    """Оценить ответ. ``payload=None`` — ответа нет (лимит шагов или времени): пустые выводы."""
    issues = truth["issues"]
    if payload is None:
        findings = []
    else:
        try:
            findings = validate_submission(payload, truth["episode_id"])
        except SubmissionError as e:
            return {"valid": False, "error": str(e), "reward": -1.0, "experimental_reward": -1.0,
                    "tp": 0, "fp": 0, "fn": len(issues), "precision": 0.0, "recall": 0.0, "f1": 0.0,
                    "inspection_steps": inspection_steps, "n_findings": None, "pairs": [],
                    "missed_issues": [t["issue_id"] for t in issues], "unmatched_findings": []}
    m = match(findings, issues, aliases)
    met = metrics(m["tp"], m["fp"], m["fn"])
    return {
        "valid": True, "error": None,
        "reward": terminal_reward(met["f1"], inspection_steps),
        "experimental_reward": experimental_reward(findings, issues, aliases, inspection_steps),
        "tp": m["tp"], "fp": m["fp"], "fn": m["fn"], **met,
        "inspection_steps": inspection_steps, "n_findings": len(findings),
        "pairs": m["pairs"], "missed_issues": m["missed_issues"], "unmatched_findings": m["unmatched_findings"],
    }
