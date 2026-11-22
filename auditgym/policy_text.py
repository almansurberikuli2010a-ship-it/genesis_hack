"""Опубликованная учётная политика (policy.json), текст задачи и справка по действиям.

Здесь сказано всё, что агенту нужно, чтобы решить, ошибочна ли запись, и вычислить
ожидаемую сумму: недоступных фактов задача не требует. Ключи JSON — латиницей
(машиночитаемые), тексты — на русском.
"""
from __future__ import annotations

from .matcher import AMOUNT_TOLERANCE_MINOR
from .posting import CHART_OF_ACCOUNTS

# ID строки курса: в fx.csv столбца нет, ID выводится из ключа строки (см. manifest.fx_id_format).
FX_ID_TEMPLATE = "FX-{date}-{source_currency}-{target_currency}"
FX_ID_FIELDS = ("date", "source_currency", "target_currency")

FINDING_TYPES = {
    "duplicate_invoice": {
        "meaning": "один и тот же счёт покупателя (тот же контрагент, валюта, момент выставления, сумма "
                   "и одинаковые строки, включая номера заказов PO) внесён дважды под разными invoice_id",
        "affected_ids": "оба invoice_id (порядок не важен)",
        "amount": "сумма в базовой валюте, проведённая по 1200 для одной копии (на столько завышена выручка)",
        "evidence": "doc_id обоих счетов и строка книги (1200 или 4000) одной из копий",
    },
    "payment_allocation": {
        "meaning": "платёж полностью расписан в invoice_refs, но при проводке расписанный счёт не закрыт по "
                   "1200: часть или вся сумма осталась на 2900 либо зачтена на другой счёт",
        "affected_ids": "[payment_id, invoice_id из invoice_refs]",
        "amount": "сумма ошибочной кредитовой строки, как она проведена (кредит 2900 или кредит 1200 по чужому счёту)",
        "evidence": "doc_id платежа и эта строка книги",
    },
    "fx_rate": {
        "meaning": "валютный документ пересчитан не по курсу политики: курс другой даты, вручную "
                   "округлённый или перевёрнутый курс",
        "affected_ids": "[invoice_id] или [payment_id]",
        "amount": "|проведённая сумма − правильно пересчитанная сумма| по основной строке "
                  "(дебет 1200 у счёта, строка 1000 у платежа)",
        "evidence": "doc_id документа, основная строка книги (у счёта подойдёт и строка 4000) и fx_id курса "
                    "на дату по политике",
    },
    "period_cutoff": {
        "meaning": "документ с моментом времени вне [period_start, period_end) — раньше начала (проведён "
                   "поздно) или после конца (проведён задним числом) — проведён внутри периода",
        "affected_ids": "[invoice_id] или [payment_id]",
        "amount": "сумма, проведённая в периоде по основной строке (дебет 1200 у счёта, дебет 1000 у платежа)",
        "evidence": "doc_id документа и основная строка книги (у счёта подойдёт и строка 4000)",
    },
}


def build_policy(manifest: dict) -> dict:
    base = manifest["base_currency"]
    return {
        "policy_version": "3.3",
        "language": "ru",
        "base_currency": base,
        "ledger_currency": "все строки книги ведутся в базовой валюте %s" % base,
        "chart_of_accounts": CHART_OF_ACCOUNTS,
        "period": {
            "start": manifest["period_start"],
            "end": manifest["period_end"],
            "membership": "полуинтервал моментов времени: start <= t < end. Сравниваются моменты, а не "
                          "напечатанные даты: документы могут нести любое смещение UTC",
            "company_timezone": manifest["timezone"],
        },
        "fx": {
            "rate_date": "календарная дата момента документа (issued_at счёта / timestamp платежа) в часовом поясе компании",
            "rate_source": "строка fx.csv с date = дата курса, source_currency = валюта документа, target_currency = "
                           "базовая валюта; её fx_id = FX-<date>-<source>-<target>",
            "fx_id_template": manifest.get("fx_id_format", {}).get("template", FX_ID_TEMPLATE),
            "conversion": "base_minor = ROUND_HALF_EVEN(amount_minor * rate * 10^(exp_base - exp_source)); "
                          "та же валюта — курс 1",
        },
        "posting_rules": {
            "invoice": "Дт 1200 / Кт 4000 по курсу даты счёта",
            "payment": "Дт 1000 по курсу даты платежа; Кт 1200 по каждой строке invoice_refs по курсу, по которому "
                       "счёт был поставлен; платёж, закрывающий счёт в ноль, списывает остаток 1200 целиком; "
                       "разница — на 7800",
            "refund": "отрицательная сумма платежа: Кт 1000 / Дт 1200 по указанному счёту по курсу, по которому "
                      "счёт был поставлен; возврат (отрицательная строка invoice_refs), закрывающий счёт в ноль, "
                      "списывает остаток 1200 целиком; разница — на 7800; переплата (строка invoice_refs больше "
                      "остатка счёта) законна и оставляет кредитовое сальдо 1200 до возврата",
            "unremitted": "платёж с invoice_refs = null корректно проводится целиком на 2900; угадывать его счёт нельзя",
            "partial_and_split": "частичная оплата, несколько платежей на один счёт и один платёж на несколько "
                                 "счетов — законны",
            "operating": "операционные проводки без документов (Дт 6xxx / Кт 1000: аренда, зарплата, комиссии и т. п.) "
                         "корректны и к типам ошибок не относятся",
            "zero_rows": "строк книги с нулевыми дебетом и кредитом нет: если по строке invoice_refs на 1200 ничего не "
                         "зачтено, строки 1200 для этого счёта нет",
            "posting_timestamp": "проводка несёт момент документа, выраженный во времени компании",
            "out_of_period_documents": "в таблицах документов могут быть документы вне периода (счета прошлого "
                                       "периода, счета и оплаты после его конца); правильно, что они в этом периоде "
                                       "НЕ проведены. Ошибка — только если такой документ проведён в периоде",
            "same_amounts": "одинаковые суммы у одного клиента (повторяющиеся счета, повторные заказы с другими "
                            "номерами PO или другим моментом выставления) законны",
        },
        "rounding": "ROUND_HALF_EVEN до минимальной единицы базовой валюты, по каждой строке книги",
        "finding_types": FINDING_TYPES,
        "finding_currency": "claimed_amount_minor всегда в базовой валюте %s; currency = %s" % (base, base),
        "evidence_ids": "ID существующих открытых записей: row_id (строки книги), doc_id (счета и платежи), fx_id (курсы; "
                        "шаблон — fx.fx_id_template); invoice_id и payment_id тоже существуют, но doc_id в наборе не "
                        "заменяют. Нужен хотя бы один полный набор, указанный в finding_types.evidence",
        "matching": {
            "normalization": {
                "type": "точное совпадение строки с одним из ключей finding_types; неизвестная строка — вывод не "
                        "сопоставляется (FP), не строка — неверная структура",
                "affected_ids": "МНОЖЕСТВО точных строк (с учётом регистра и пробелов, без синонимов doc_id ⇄ invoice_id/"
                                "payment_id): порядок и повторы не важны; должно точно совпасть с каноническим набором "
                                "ошибки (finding_types.<type>.affected_ids); лишний или недостающий ID — несовпадение",
                "currency": "точное совпадение строки с кодом валюты эталона (base_currency): без смены регистра и "
                            "обрезки пробелов",
                "claimed_amount_minor": "JSON-целое (bool, строка и дробное число — неверная структура); совпадает, если "
                                        "|claimed − expected| <= amount_tolerance_minor[type]",
                "evidence_ids": "сначала лимит 10 элементов исходного списка (повторы считаются), затем МНОЖЕСТВО точных "
                                "строк; каждый ID обязан существовать в открытых записях эпизода (evidence_ids_rule."
                                "known_ids), иначе вывод не сопоставляется (FP); вывод подтверждён, если множество "
                                "содержит хотя бы один полный допустимый набор; лишние существующие ID не мешают",
                "explanation": "строка до 1000 символов (кодовых точек Unicode); на сопоставление и награду не влияет",
            },
            "affected_ids_rule": {"compare_as": "set", "exact": True, "strip": False, "case_sensitive": True,
                                  "duplicates": "ignored", "order": "ignored", "canonical": ["invoice_id", "payment_id"],
                                  "aliases": {}},
            "evidence_ids_rule": {"compare_as": "set", "exact": True, "strip": False, "limit_before_dedup": 10,
                                  "accepted_if": "superset_of_any_allowed_set",
                                  "known_ids": ["row_id", "doc_id", "invoice_id", "payment_id", "fx_id"],
                                  "unknown_id": "вывод не сопоставляется (FP)", "aliases": {}},
            "amount_tolerance_minor": dict(AMOUNT_TOLERANCE_MINOR),
            "amount_rule": "abs(claimed_amount_minor − expected_amount_minor) <= amount_tolerance_minor[type]; "
                           "τ фиксирован заранее, по умолчанию 0 — точное совпадение целых",
            "structure_errors": "только: неверный тип значения, отсутствует обязательное поле (episode_id, findings, "
                                "шесть полей вывода), больше 30 выводов, больше 10 evidence_ids в выводе, explanation "
                                "длиннее 1000 символов, episode_id не равен manifest.episode_id. Лишние ключи "
                                "игнорируются. Неверная структура завершает эпизод с наградой −1",
            "one_to_one": "каждый вывод сопоставляется не более чем с одной ошибкой и наоборот",
            "tie_break": "паросочетание максимальной мощности; среди равных — лексикографически наименьшая "
                         "последовательность пар (issue_id, индекс вывода с 0), отсортированная по issue_id "
                         "(сравнение строк по кодовым точкам Unicode)",
            "duplicates_and_unmatched": "повторные и несопоставленные выводы — ложные срабатывания (FP)",
            "code": "auditgym/matcher.py",
        },
        "limits": {"max_findings": 30, "max_evidence_ids": 10, "max_explanation_chars": 1000,
                   "page_size_max": 50, "max_steps": 100, "time_limit_s": 60},
    }


TASK_TEXT = (
    "Вы проводите аудит одной компании за один отчётный период. Записи доступны только для чтения и "
    "только через инструменты. Найдите расхождения, описанные в policy.finding_types, подкрепите каждое "
    "идентификаторами доказательств и один раз вызовите submit. Разбитые, частичные, общие, "
    "нераспределённые платежи, возвраты, повторяющиеся суммы, документы соседних периодов и "
    "операционные проводки сами по себе ошибками не являются. Бывают и чистые компании — тогда "
    "правильный ответ — пустой список выводов. Каждое действие тратит один шаг; эпизод заканчивается "
    "отправкой ответа или исчерпанием бюджета (тогда ответ считается пустым)."
)

ACTIONS_HELP = {
    "list_records": {"table": "ledger | invoices | payments | fx", "cursor": "строка или null",
                     "limit": "1..50 (больше обрезается до 50)"},
    "get_record": {"table": "ledger | invoices | payments | fx", "id": "row_id | invoice_id/doc_id | payment_id/doc_id | fx_id"},
    "get_document": {"doc_id": "doc_id счёта или платежа"},
    "calculator": {"expression": "десятичная арифметика: + - * / // % ( )"},
    "submit": {"findings": {"episode_id": "строка", "findings": [
        {"type": "duplicate_invoice | payment_allocation | fx_rate | period_cutoff", "affected_ids": ["строка"],
         "claimed_amount_minor": "целое", "currency": "код валюты", "evidence_ids": ["строка"],
         "explanation": "короткое обоснование (до 1000 символов)"}]}},
}
