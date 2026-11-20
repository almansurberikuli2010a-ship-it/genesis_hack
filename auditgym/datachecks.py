"""Проверки данных и эталона одного эпизода («Данные и эталон»).

Эпизод корректен, если:
 1. книги до внесения ошибок, ВОССТАНОВЛЕННЫЕ ИЗ provenance.json, совпадают со снимком и
    выполняют все бухгалтерские равенства; у каждого изменения книги есть записанная причина;
 2. книги после внесения сохраняют структурные равенства (баланс, сбалансированные проводки,
    расписки равны суммам, книга внутри периода и в базовой валюте); независимая ПЕРЕПРОВОДКА
    каждого документа по политике совпадает с чистой книгой полностью, а с книгой после
    внесения — везде, кроме документов объявленных ошибок и платежей по затронутым счетам;
 3. каждое нарушение сальдо 1200 и каждое «проведено вне периода» объявлено в эталоне;
 4. эталонный аудитор не находит ничего на чистых книгах (отвлекающие случаи корректны);
 5. эталонный аудитор воспроизводит эталон точно (F1 = 1): всё объявленное обнаружимо по
    открытым записям, необъявленного нет;
 6. каждый набор доказательств каждой ошибки сам по себе доказывает её;
 7. счётчики manifest совпадают, закрытые значения не встречаются в открытых записях.
Все проверки работают и по экспортированным ФАЙЛАМ (check_records), без генератора.
"""
from __future__ import annotations

import json

from .audit_rules import (book_invariants, posting_breaks, repost_differences, rule_audit, unsettled_invoices,
                          verify_evidence)
from .matcher import build_aliases, score
from .provenance_tools import reconstruct_pre_injection, same_books, unattributed_changes

GRADER_ONLY_KEYS = ("expected_amount_minor", "evidence_sets", "mutation_id", "ledger_diff", "story_hash",
                    "canonical_ids", "tolerance_minor")


def check_episode(ep) -> list[str]:
    return check_records(ep.public, ep.clean_public, ep.truth, ep.provenance)


def check_records(pub: dict, clean: dict, truth: dict, provenance: dict) -> list[str]:
    problems = []
    rebuilt = reconstruct_pre_injection(pub, provenance)
    problems += ["обращение provenance: " + x for x in same_books(rebuilt, clean)]
    problems += ["provenance: " + x for x in unattributed_changes(provenance)]
    problems += ["книги до внесения: " + v for v in book_invariants(rebuilt, subledger=True)]
    problems += ["книги после внесения: " + v for v in book_invariants(pub, subledger=False)]
    # независимая перепроводка: чистые книги совпадают с политикой полностью; в книгах с
    # ошибками отличаются только документы объявленных ошибок и платежи по затронутым счетам
    problems += ["перепроводка чистых книг: " + v for v in repost_differences(rebuilt)]
    affected = {a for t in truth["issues"] for a in t["affected_ids"]}
    consequence = {p["payment_id"] for p in pub["payments"]
                   if any(x["invoice_id"] in affected for x in p["invoice_refs"] or [])}
    problems += ["перепроводка после внесения: " + v for v in repost_differences(pub, affected | consequence)]
    declared_alloc = {a for t in truth["issues"] if t["type"] == "payment_allocation" for a in t["affected_ids"]}
    for inv in sorted(unsettled_invoices(pub) - declared_alloc):
        problems.append("необъявленное нарушение сальдо 1200 по счёту %s" % inv)
    declared_cut = {a for t in truth["issues"] if t["type"] == "period_cutoff" for a in t["affected_ids"]}
    for kind, key in posting_breaks(pub):
        if key not in declared_cut:
            problems.append("необъявленное нарушение «проведён ⇔ в периоде»: %s %s" % (kind, key))
    for f in rule_audit(clean):
        problems.append("эталонный аудитор нашёл «ошибку» в чистых книгах: %s %s" % (f["type"], f["affected_ids"]))
    s = score({"episode_id": pub["manifest"]["episode_id"], "findings": rule_audit(pub)[:30]}, truth, build_aliases(pub), 0)
    if s["f1"] != 1.0:
        problems.append("эталонный аудитор не воспроизводит эталон: tp=%d fp=%d fn=%d" % (s["tp"], s["fp"], s["fn"]))
    for t in truth["issues"]:
        for p in verify_evidence(t, pub):
            problems.append("доказательства %s (%s): %s" % (t["issue_id"], t["type"], p))
        if t["expected_amount_minor"] <= 0:
            problems.append("неположительная ожидаемая сумма")
    c = pub["manifest"]["counts"]
    for k in ("ledger", "invoices", "payments", "fx"):
        if c[k] != len(pub[k]):
            problems.append("счётчик manifest не совпадает для %s" % k)
    blob = json.dumps(pub, ensure_ascii=False)
    for t in truth["issues"]:
        if t["issue_id"] in blob:
            problems.append("issue_id попал в открытые записи")
    for key in GRADER_ONLY_KEYS:
        if '"%s"' % key in blob:
            problems.append("закрытый ключ %r найден в открытых записях" % key)
    muts = {m["mutation_id"] for m in provenance["mutations"]}
    for t in truth["issues"]:
        if t["mutation_id"] not in muts:
            problems.append("ошибка без записанного причинного изменения")
    return problems
