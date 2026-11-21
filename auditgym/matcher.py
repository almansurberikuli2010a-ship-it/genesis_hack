"""Общий matcher и терминальная награда (раздел 05 ТЗ). Код открыт и опубликован; правила —
по «Точным соглашениям matcher» определений проверок жюри.

Структура (validate_submission)
-------------------------------
Неверная структура всего submit (награда −1, ошибка — в журнале оценщика) — ТОЛЬКО:
  * неверный тип значения: findings.json не объект; episode_id не строка; findings не список;
    вывод не объект; type, currency, explanation не строки; affected_ids, evidence_ids не списки
    строк; claimed_amount_minor не JSON-целое (bool, строка и дробное число — неверный тип);
  * отсутствует обязательное поле (episode_id, findings и шесть полей вывода);
  * превышен лимит: больше 30 выводов, больше 10 элементов ИСХОДНОГО списка evidence_ids
    (до удаления повторов), explanation длиннее 1000 символов (кодовых точек Unicode после
    разбора JSON);
  * episode_id не равен episode_id эпизода (manifest.episode_id).
Лишние ключи игнорируются. Пустые, повторные, несуществующие ID, неизвестная строка type и
любая валюта — НЕ ошибка структуры: такой вывод просто не сопоставляется (FP).

Нормализация и совместимость (вывод f может быть TP для ошибки эталона t)
------------------------------------------------------------------------
* ID сравниваются точно: с учётом регистра и пробелов, без синонимов doc_id ⇄ invoice_id /
  payment_id, без приведения чисел к строкам;
* type — точное совпадение с одним из FINDING_TYPES;
* affected_ids — МНОЖЕСТВО (порядок и повторы не важны), точно равное каноническому набору
  t.affected_ids (канонические ID по типам — policy.finding_types.<type>.affected_ids);
* claimed_amount_minor: |claimed − expected| ≤ τ, τ = t.tolerance_minor (генератор пишет
  τ = AMOUNT_TOLERANCE_MINOR[type] = 0, опубликован в policy.json → matching);
* currency — точное совпадение строки с t.currency (без смены регистра и обрезки пробелов);
* evidence_ids — МНОЖЕСТВО; каждый ID обязан существовать в открытых записях эпизода
  (EVIDENCE_ID_FIELDS: row_id, doc_id, invoice_id, payment_id, fx_id), иначе вывод не
  сопоставляется ни с чем; хотя бы один допустимый набор t.evidence_sets входит целиком.
  invoice_id / payment_id — существующие ID записей: допустимы как дополнительные, но doc_id
  в наборе не заменяют.
explanation в сопоставлении не участвует.

Неоднозначность
---------------
Двудольный граф: слева выводы (повторы — отдельные вершины), справа ошибки эталона; ребро —
все условия TP. Берётся паросочетание максимальной мощности; среди них — с лексикографически
наименьшей последовательностью пар (issue_id, индекс вывода с 0), отсортированной по issue_id
(строки сравниваются по кодовым точкам Unicode). TP — число пар, FP = выводов − TP,
FN = ошибок − TP.

Метрики и награда
-----------------
P = TP/(TP+FP), R = TP/(TP+FN), F1 = 2TP/(2TP+FP+FN); при нулевом знаменателе P или R = 0;
эталон и ответ пусты → F1 = 1; эталон пуст, ответ нет → F1 = 0.
Награда = clip(2·F1 − 1 − 0.002·inspection_steps, −1, 1).
"""
from __future__ import annotations

FINDING_TYPES = ("duplicate_invoice", "payment_allocation", "fx_rate", "period_cutoff")
FINDING_KEYS = ("type", "affected_ids", "claimed_amount_minor", "currency", "evidence_ids", "explanation")
MAX_FINDINGS = 30
MAX_EVIDENCE = 10
MAX_EXPLANATION = 1000
STEP_COST = 0.002
# Опубликованный допуск суммы по типам (неотрицательное целое, минимальные единицы). По умолчанию 0:
# суммы эталона — точный пересчёт по policy.json, документированного округления сверх него нет.
AMOUNT_TOLERANCE_MINOR = {t: 0 for t in FINDING_TYPES}
# Поля открытых записей, ID из которых принимаются в evidence_ids (fx_id — ключ строки курса,
# собранный по manifest.fx_id_format из date, source_currency и target_currency).
EVIDENCE_ID_FIELDS = (("ledger", "row_id"), ("invoices", "doc_id"), ("invoices", "invoice_id"),
                      ("payments", "doc_id"), ("payments", "payment_id"), ("fx", "fx_id"))


class SubmissionError(ValueError):
    """Нарушение структуры findings.json."""


def _str_list(v, what):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise SubmissionError("%s должен быть списком строк" % what)
    return list(v)


def validate_submission(obj, episode_id: str) -> list[dict]:
    """Выбросить SubmissionError при структурной ошибке; вернуть выводы (только шесть полей)."""
    if not isinstance(obj, dict):
        raise SubmissionError("findings должен быть объектом")
    for k in ("episode_id", "findings"):
        if k not in obj:
            raise SubmissionError("нет обязательного поля %s" % k)
    if not isinstance(obj["episode_id"], str):
        raise SubmissionError("episode_id должен быть строкой")
    if obj["episode_id"] != episode_id:
        raise SubmissionError("episode_id не совпадает с эпизодом")
    fs = obj["findings"]
    if not isinstance(fs, list):
        raise SubmissionError("findings.findings должен быть списком")
    if len(fs) > MAX_FINDINGS:
        raise SubmissionError("не больше %d выводов" % MAX_FINDINGS)
    out = []
    for i, f in enumerate(fs):
        if not isinstance(f, dict):
            raise SubmissionError("вывод %d должен быть объектом" % i)
        missing = [k for k in FINDING_KEYS if k not in f]
        if missing:
            raise SubmissionError("вывод %d: нет поля %s" % (i, missing[0]))
        for k in ("type", "currency", "explanation"):
            if not isinstance(f[k], str):
                raise SubmissionError("вывод %d: %s должен быть строкой" % (i, k))
        amt = f["claimed_amount_minor"]
        if not isinstance(amt, int) or isinstance(amt, bool):
            raise SubmissionError("вывод %d: claimed_amount_minor должен быть целым числом" % i)
        affected = _str_list(f["affected_ids"], "вывод %d: affected_ids" % i)
        evidence = _str_list(f["evidence_ids"], "вывод %d: evidence_ids" % i)
        if len(evidence) > MAX_EVIDENCE:  # лимит — по исходному списку, до удаления повторов
            raise SubmissionError("вывод %d: не больше %d evidence_ids" % (i, MAX_EVIDENCE))
        if len(f["explanation"]) > MAX_EXPLANATION:  # len(str) — число кодовых точек Unicode
            raise SubmissionError("вывод %d: explanation длиннее %d символов" % (i, MAX_EXPLANATION))
        out.append({"type": f["type"], "affected_ids": affected, "claimed_amount_minor": amt,
                    "currency": f["currency"], "evidence_ids": evidence, "explanation": f["explanation"]})
    return out


def match_context(public: dict) -> dict:
    """Открытая часть, нужная сопоставлению: episode_id эпизода и множество существующих ID
    записей, допустимых в evidence_ids. Строится только из открытых записей."""
    known = set()
    for table, field in EVIDENCE_ID_FIELDS:
        known.update(r[field] for r in public[table] if isinstance(r.get(field), str))
    return {"episode_id": public["manifest"]["episode_id"], "evidence_ids": frozenset(known)}


build_aliases = match_context  # прежнее имя (синонимов ID больше нет); оставлено для совместимости вызовов


def _norm(f, ctx):
    evidence = frozenset(f["evidence_ids"])
    return {
        "type": f["type"],
        "affected": frozenset(f["affected_ids"]),
        "amount": f["claimed_amount_minor"],
        "currency": f["currency"],
        "evidence": evidence,
        "evidence_known": evidence <= ctx["evidence_ids"],
    }


def tolerance(issue: dict) -> int:
    """τ ошибки эталона: её tolerance_minor (генератор пишет опубликованный AMOUNT_TOLERANCE_MINOR)."""
    return int(issue.get("tolerance_minor", AMOUNT_TOLERANCE_MINOR.get(issue["type"], 0)))


def compatible(nf: dict, issue: dict) -> bool:
    if nf["type"] != issue["type"]:
        return False
    if nf["affected"] != frozenset(issue["affected_ids"]):
        return False
    if nf["currency"] != issue["currency"]:
        return False
    if abs(nf["amount"] - issue["expected_amount_minor"]) > tolerance(issue):
        return False
    if not nf["evidence_known"]:
        return False
    return any(frozenset(es) <= nf["evidence"] for es in issue["evidence_sets"])


def _matching_size(adj, n_issues, findings, issues) -> int:
    """Мощность максимального паросочетания (алгоритм Куна) на подграфе: выводы ``findings``,
    ошибки ``issues`` (множества индексов)."""
    owner = {}

    def try_assign(i, seen):
        for j in adj[i]:
            if j not in issues or j in seen:
                continue
            seen.add(j)
            if j not in owner or try_assign(owner[j], seen):
                owner[j] = i
                return True
        return False

    return sum(1 for i in sorted(findings) if try_assign(i, set()))


def select_pairs(adj: list[list[int]], issue_ids: list[str]) -> list[tuple[str, int]]:
    """Опубликованный выбор при неоднозначности. ``adj[i]`` — индексы ошибок, совместимых с выводом i;
    ``issue_ids[j]`` — issue_id ошибки j. Возвращает пары (issue_id, индекс вывода) паросочетания
    максимальной мощности с лексикографически наименьшей последовательностью этих пар (по issue_id)."""
    n_f = len(adj)
    order = sorted(range(len(issue_ids)), key=lambda j: issue_ids[j])
    k = _matching_size(adj, len(issue_ids), set(range(n_f)), set(order))
    cands = {j: sorted(i for i in range(n_f) if j in adj[i]) for j in order}
    free_f, pairs = set(range(n_f)), []
    for pos, j in enumerate(order):
        later = set(order[pos + 1:])
        for i in cands[j]:
            if i not in free_f:
                continue
            if len(pairs) + 1 + _matching_size(adj, len(issue_ids), free_f - {i}, later) == k:
                pairs.append((issue_ids[j], i))
                free_f.discard(i)
                break
    return pairs


def match(findings: list[dict], issues: list[dict], ctx: dict) -> dict:
    norm = [_norm(f, ctx) for f in findings]
    adj = [[j for j, t in enumerate(issues) if compatible(nf, t)] for nf in norm]
    chosen = select_pairs(adj, [t["issue_id"] for t in issues])
    pairs = sorted((i, iid) for iid, i in chosen)
    matched_f, matched_t = {i for i, _ in pairs}, {iid for _, iid in pairs}
    return {
        "tp": len(pairs),
        "fp": len(findings) - len(pairs),
        "fn": len(issues) - len(pairs),
        "pairs": pairs,
        "unmatched_findings": [i for i in range(len(findings)) if i not in matched_f],
        "missed_issues": sorted(t["issue_id"] for t in issues if t["issue_id"] not in matched_t),
    }


def metrics(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 1.0 if tp + fp + fn == 0 else 2 * tp / (2 * tp + fp + fn)
    return {"precision": p, "recall": r, "f1": f1}


def terminal_reward(f1: float, inspection_steps: int) -> float:
    return max(-1.0, min(1.0, 2.0 * f1 - 1.0 - STEP_COST * inspection_steps))


def experimental_reward(findings, issues, ctx, inspection_steps) -> float:
    """ЭКСПЕРИМЕНТАЛЬНАЯ награда (НЕ общая): половина за вывод с верными типом и ID, но неверной
    суммой, валютой или доказательствами. Показывается отдельно и никогда не подменяет общую."""
    norm = [_norm(f, ctx) for f in findings]
    m = match(findings, issues, ctx)
    credit = float(m["tp"])
    used = {i for i, _ in m["pairs"]}
    open_issues = set(m["missed_issues"])
    for i, nf in enumerate(norm):
        if i in used:
            continue
        for t in sorted(issues, key=lambda x: x["issue_id"]):
            if t["issue_id"] in open_issues and nf["type"] == t["type"] and nf["affected"] == frozenset(t["affected_ids"]):
                credit += 0.5
                open_issues.discard(t["issue_id"])
                break
    n_f, n_t = len(findings), len(issues)
    f1 = 1.0 if n_f == n_t == 0 else (2 * credit / (n_f + n_t) if n_f + n_t else 0.0)
    return terminal_reward(f1, inspection_steps)


def score(payload, truth: dict, ctx: dict, inspection_steps: int) -> dict:
    """Оценить ответ. ``payload=None`` — ответа нет (лимит шагов или времени): пустые выводы.
    ``ctx`` — match_context(открытые записи эпизода)."""
    issues = truth["issues"]
    if payload is None:
        findings = []
    else:
        try:
            findings = validate_submission(payload, ctx.get("episode_id", truth["episode_id"]))
        except SubmissionError as e:
            return {"valid": False, "error": str(e), "reward": -1.0, "experimental_reward": -1.0,
                    "tp": 0, "fp": 0, "fn": len(issues), "precision": 0.0, "recall": 0.0, "f1": 0.0,
                    "inspection_steps": inspection_steps, "n_findings": None, "pairs": [],
                    "missed_issues": sorted(t["issue_id"] for t in issues), "unmatched_findings": []}
    m = match(findings, issues, ctx)
    met = metrics(m["tp"], m["fp"], m["fn"])
    return {
        "valid": True, "error": None,
        "reward": terminal_reward(met["f1"], inspection_steps),
        "experimental_reward": experimental_reward(findings, issues, ctx, inspection_steps),
        "tp": m["tp"], "fp": m["fp"], "fn": m["fn"], **met,
        "inspection_steps": inspection_steps, "n_findings": len(findings),
        "pairs": m["pairs"], "missed_issues": m["missed_issues"], "unmatched_findings": m["unmatched_findings"],
    }
