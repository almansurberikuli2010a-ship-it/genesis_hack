"""Репетиция оценки жюри (раздел 10 ТЗ) — САМООЦЕНКА.

Определения проверок публикует жюри; здесь — наше прочтение каждого критерия в виде
проверок «чёрного ящика»:

* контракт и изоляция  — 20 проверок против среды в ОТДЕЛЬНОМ процессе (SubprocessEnv);
* данные и эталон      — 20 свежих «скрытых» эпизодов (5 чистых, 5 с взаимодействующими
                          ошибками, 10 смешанных); эпизод пишется в файлы и проверяется ТОЛЬКО
                          по файлам;
* надёжность награды   — 20 проверок, каждый ответ отправляется через среду; допуск берётся
                          из опубликованной формулы, а не из кода;
* воспроизводимость    — 20 парных проверок: 10 точных повторов, 5 смен ID и порядка через
                          конфиг, 5 внешних преобразований файлов;
* инженерия            — 5 пунктов, проверка наличия (0/1/2 каждый).
"""
from __future__ import annotations

import copy
import json
import tempfile
import time
from pathlib import Path

from .datachecks import check_records
from .env import MAX_STEPS
from .export import ENC, load_episode_files, write_episode
from .generator import generate
from .policies import make_policy
from .runner import ROOT, run_episode
from .server import SubprocessEnv
from .transform import transform_episode

HIDDEN_BASE = 7_000_000
GRADER_KEYS = {"issue_id", "issues", "expected_amount_minor", "evidence_sets", "mutation_id", "mutations",
               "story_hash", "truth", "tolerance_minor", "ledger_diff", "canonical_ids"}


def published_tolerance(expected: int) -> int:
    """Допуск ровно по опубликованной формуле (не из кода): max(1, floor(|expected| × 0.0005))."""
    return max(1, abs(int(expected)) * 5 // 10000)


def _keys(o, acc):
    if isinstance(o, dict):
        for k, v in o.items():
            acc.add(k)
            _keys(v, acc)
    elif isinstance(o, list):
        for v in o:
            _keys(v, acc)
    return acc


def hidden_suite():
    """20 «скрытых» эпизодов: 5 чистых, 5 с взаимодействующими ошибками, 10 смешанных."""
    presets = ["clean", "clean_hard", "clean", "clean_hard", "clean",
               "interacting", "interacting_partial_fx_boundary", "interacting_masked_duplicate", "interacting", "heavy",
               "four_types", "mixed", "only_duplicate_invoice", "only_payment_allocation", "only_fx_rate",
               "only_period_cutoff", "mixed", "four_types", "heavy", "mixed"]
    return [{"name": "скрытый%02d_%s" % (i, p), "seed": HIDDEN_BASE + 7919 * i, "config": {"preset": p}}
            for i, p in enumerate(presets)]


class _Checks:
    def __init__(self):
        self.rows = []

    def __call__(self, name, fn):
        t = time.perf_counter()
        try:
            ok, detail = fn()
        except Exception as e:  # падение проверки — проваленная проверка, а не упавшая репетиция
            ok, detail = False, "исключение: %r" % (e,)
        self.rows.append({"check": name, "passed": bool(ok), "detail": detail, "time_s": round(time.perf_counter() - t, 3)})


# ------------------------------------------------------------------ контракт и изоляция

def contract_checks(seed=HIDDEN_BASE + 1, cfg=None) -> list[dict]:
    cfg = cfg or {"preset": "mixed"}
    c = _Checks()

    def fresh(mode="train", **kw):
        e = SubprocessEnv(mode=mode, **kw)
        obs, info = e.reset(seed, cfg)
        return e, obs, info

    def with_env(fn, mode="train", **kw):
        def run():
            e, obs, info = fresh(mode, **kw)
            try:
                return fn(e, obs, info)
            finally:
                e.close()
        return run

    def t_reset(e, obs, info):
        json.dumps(obs), json.dumps(info)
        return isinstance(obs, dict) and isinstance(info, dict), "reset → (dict, dict), сериализуется в JSON"

    def t_first(e, obs, info):
        return {"task", "manifest", "policy", "steps_remaining"} <= set(obs) and obs["steps_remaining"] == MAX_STEPS, \
            "первое наблюдение: задача, manifest, политика, бюджет %d" % obs["steps_remaining"]

    def t_manifest(e, obs, info):
        need = {"schema_version", "episode_id", "base_currency", "period_start", "period_end", "timezone", "counts", "rounding"}
        return need <= set(obs["manifest"]), "поля manifest по разделу 03"

    def t_step_types(e, obs, info):
        out = e.step({"type": "list_records", "table": "ledger"})
        types = [type(x).__name__ for x in out]
        return types == ["dict", "float", "bool", "bool", "dict"] and out[1] == 0.0, "step → %s" % types

    def t_page(e, obs, info):
        o = e.step({"type": "list_records", "table": "ledger", "limit": 1000})[0]
        return len(o["result"]["rows"]) <= 50, "limit=1000 → %d строк" % len(o["result"]["rows"])

    def t_paging(e, obs, info):
        total, seen, cur = obs["manifest"]["counts"]["ledger"], [], None
        while True:
            o = e.step({"type": "list_records", "table": "ledger", "cursor": cur})[0]
            seen += [r["row_id"] for r in o["result"]["rows"]]
            cur = o["result"]["next_cursor"]
            if cur is None:
                break
        a = e.step({"type": "list_records", "table": "ledger", "cursor": "ledger:50"})[0]["result"]
        b = e.step({"type": "list_records", "table": "ledger", "cursor": "ledger:50"})[0]["result"]
        return len(seen) == total == len(set(seen)) and a == b, "полный обход %d/%d строк, курсор стабилен" % (len(set(seen)), total)

    def t_get(e, obs, info):
        led = e.step({"type": "list_records", "table": "ledger", "limit": 1})[0]["result"]["rows"][0]
        inv = e.step({"type": "list_records", "table": "invoices", "limit": 1})[0]["result"]["rows"][0]
        fx = e.step({"type": "list_records", "table": "fx", "limit": 1})[0]["result"]["rows"][0]
        ok = (e.step({"type": "get_record", "table": "ledger", "id": led["row_id"]})[0]["result"]["record"] == led
              and e.step({"type": "get_document", "doc_id": inv["doc_id"]})[0]["result"]["document"] == inv
              and e.step({"type": "get_record", "table": "fx", "id": fx["fx_id"]})[0]["result"]["record"] == fx)
        return ok, "get_record / get_document возвращают запрошенную запись"

    def bounded(action, code):
        def fn(e, obs, info):
            o, r, te, tr, inf = e.step(action)
            ok = (o["error"] is not None and o["error"]["code"] == code and o["result"] is None
                  and o["steps_remaining"] == MAX_STEPS - 1 and len(o["error"]["message"]) < 120 and (r, te, tr) == (0.0, False, False))
            return ok, "%s → %s, шаг израсходован" % (json.dumps(action, ensure_ascii=False)[:60], o["error"])
        return fn

    def t_calc(e, obs, info):
        ok = e.step({"type": "calculator", "expression": "(1352.875 - 1000) * 3 / 4"})[0]["result"]["value"] == "264.65625"
        for bad in ("__import__('os').system('id')", "open('/etc/passwd').read()", "2**10**10", "x"):
            o = e.step({"type": "calculator", "expression": bad})[0]
            ok &= o["error"] is not None and o["error"]["code"] == "calculator_error"
        return ok, "только десятичная арифметика; код и файлы отклоняются"

    def t_no_shell(e, obs, info):
        probes = [{"type": "get_record", "table": "ledger", "id": "../grader/truth.json"},
                  {"type": "get_document", "doc_id": "/etc/passwd"}, {"type": "list_records", "table": "truth"},
                  {"type": "shell", "cmd": "ls"}, {"type": "get_record", "table": "ledger", "id": "1 OR 1=1"}]
        outs = [e.step(p)[0] for p in probes]
        return all(o["error"] is not None and o["result"] is None for o in outs), "нет shell, SQL и файловой системы"

    def t_submit(e, obs, info):
        _, r, te, tr, _ = e.step({"type": "submit", "findings": {"episode_id": obs["episode_id"], "findings": []}})
        return te is True and tr is False, "submit → terminated=True"

    def t_steps(e, obs, info):
        for i in range(MAX_STEPS):
            o, r, te, tr, _ = e.step({"type": "calculator", "expression": "1+1"})
            if te or tr:
                break
        rep = e.grader_report()["result"]
        return tr and not te and i == MAX_STEPS - 1 and rep["end_reason"] == "step_limit" and rep["n_findings"] == 0, \
            "100-й шаг → truncated, пустой ответ"

    def t_time(e, obs, info):
        time.sleep(0.7)
        o, r, te, tr, _ = e.step({"type": "calculator", "expression": "1"})
        rep = e.grader_report()["result"]
        return tr and not te and rep["end_reason"] == "time_limit" and rep["n_findings"] == 0, "лимит времени → truncated, пустой ответ"

    def t_after_end(e, obs, info):
        e.step({"type": "submit", "findings": {"episode_id": obs["episode_id"], "findings": []}})
        first = e.grader_report()["result"]
        o, r, te, tr, _ = e.step({"type": "submit", "findings": e.oracle_findings()})
        return o["error"]["code"] == "episode_finished" and r == 0.0 and e.grader_report()["result"] == first, \
            "шаг после конца ничего не меняет"

    def t_invalid(e, obs, info):
        o, r, te, tr, _ = e.step({"type": "submit", "findings": {"episode_id": obs["episode_id"], "findings": [{"type": "fx_rate"}]}})
        log = e.grader_report()["log"]
        return (r == -1.0 and te and log and log[0]["event"] == "invalid_submission"
                and o["result"] == {"submitted": True} and o["error"] is None), "неверная структура → −1, ошибка только в журнале оценщика"

    def t_eval(e, obs, info):
        o, r, te, tr, inf = e.step({"type": "submit", "findings": e.oracle_findings()})
        return r == 0.0 and inf == {} and e.grader_report()["result"]["reward"] == 1.0, "eval: награда и диагностика у оценщика"

    def t_train(e, obs, info):
        rs = [e.step({"type": "list_records", "table": t})[1] for t in ("ledger", "fx")]
        _, r, te, _, _ = e.step({"type": "submit", "findings": e.oracle_findings()})
        return rs == [0.0, 0.0] and abs(r - (1 - 0.002 * 2)) < 1e-12 and r == e.grader_report()["result"]["reward"], \
            "train: 0 при исследовании, терминальная награда в конце"

    def t_leak(e, obs, info):
        blobs = [obs, info]
        for t in ("ledger", "invoices", "payments", "fx"):
            cur = None
            while True:
                o = e.step({"type": "list_records", "table": t, "cursor": cur})
                blobs.append(o)
                cur = o[0]["result"]["next_cursor"]
                if cur is None:
                    break
        blobs.append(e.step({"type": "get_record", "table": "ledger", "id": "truth"}))
        blobs.append(e.step({"type": "submit", "findings": {"episode_id": obs["episode_id"], "findings": []}}))
        text = json.dumps(blobs, ensure_ascii=False)
        leaked = _keys(blobs, set()) & GRADER_KEYS
        issue_ids = [i for i in json.dumps(e.grader_report()).split('"') if i.startswith("ISS-")]
        e2, o2, _ = fresh(mode="eval")
        same = o2 == obs
        e2.close()
        ok = not leaked and not any(i in text for i in issue_ids) and "grader" not in text and "provenance" not in text and same
        return ok, "нет закрытых ключей, issue_id и путей в %d наблюдениях; reset изолирует" % len(blobs)

    c("reset возвращает (obs, info)", with_env(t_reset))
    c("содержимое первого наблюдения", with_env(t_first))
    c("схема manifest", with_env(t_manifest))
    c("типы 5-кортежа step", with_env(t_step_types))
    c("страница ≤ 50 строк", with_env(t_page))
    c("стабильные курсоры, полный обход", with_env(t_paging))
    c("get_record / get_document", with_env(t_get))
    c("неизвестный ID → ограниченная ошибка, шаг израсходован",
      with_env(bounded({"type": "get_record", "table": "ledger", "id": "L-NOPE"}, "not_found")))
    c("неизвестное действие → ограниченная ошибка, шаг израсходован",
      with_env(bounded({"type": "delete_records"}, "unknown_action")))
    c("неверные аргументы → ограниченная ошибка, шаг израсходован",
      with_env(bounded({"type": "list_records", "table": "ledger", "limit": -5}, "invalid_arguments")))
    c("песочница калькулятора", with_env(t_calc))
    c("нет shell/SQL/файловой системы", with_env(t_no_shell))
    c("submit завершает эпизод", with_env(t_submit))
    c("лимит 100 шагов → truncated", with_env(t_steps))
    c("лимит времени → truncated", with_env(t_time, time_limit_s=0.5))
    c("после конца и повторная отправка — без изменений", with_env(t_after_end))
    c("неверная структура → −1 и журнал оценщика", with_env(t_invalid))
    c("режим eval скрывает награду", with_env(t_eval, mode="eval"))
    c("режим train: только терминальная награда", with_env(t_train))
    c("нет утечек, reset изолирует", with_env(t_leak, mode="eval"))
    return c.rows


# ------------------------------------------------------------------ данные и эталон

def data_checks(workdir: Path) -> list[dict]:
    c = _Checks()
    for e in hidden_suite():
        def fn(e=e):
            ep = generate(e["seed"], e["config"])
            root = workdir / ("e%d" % e["seed"])
            write_episode(ep, root)
            pub, clean, truth, prov = load_episode_files(root, ep.episode_id)  # дальше — только файлы
            problems = check_records(pub, clean, truth, prov)
            return not problems, "%d ошибок, %d строк книги%s" % (len(truth["issues"]), len(pub["ledger"]),
                                                                   "" if not problems else ": " + "; ".join(problems[:3]))
        c(e["name"], fn)
    return c.rows


# ------------------------------------------------------------------ надёжность награды

def _grade(seed, cfg, build, pre_steps=0, time_limit_s=60.0, sleep_s=0.0):
    """Отправить через среду ответ, построенный из ответа oracle; вернуть результат оценщика."""
    e = SubprocessEnv(mode="train", time_limit_s=time_limit_s)
    try:
        obs, _ = e.reset(seed, cfg)
        oracle = e.oracle_findings()
        for _ in range(pre_steps):
            e.step({"type": "calculator", "expression": "1"})
        if sleep_s:
            time.sleep(sleep_s)
        e.step({"type": "submit", "findings": build(copy.deepcopy(oracle), obs, e)})
        return e.grader_report()["result"]
    finally:
        e.close()


def reward_checks() -> list[dict]:
    c = _Checks()
    err = (HIDDEN_BASE + 11, {"preset": "four_types"})
    clean = (HIDDEN_BASE + 12, {"preset": "clean"})
    inter = (HIDDEN_BASE + 13, {"preset": "interacting"})
    same = lambda o, obs, e: o
    silence = lambda o, obs, e: {"episode_id": o["episode_id"], "findings": []}

    def g(ep, fn, steps=0, **kw):
        return _grade(ep[0], ep[1], fn, steps, **kw)

    def first_with(ep, mut):
        def fn(o, obs, e):
            o["findings"] = [mut(copy.deepcopy(o["findings"][0]), obs)]
            return o
        return g(ep, fn)

    def false_accusation(o, obs, e):
        return {"episode_id": o["episode_id"], "findings": [{"type": "duplicate_invoice", "affected_ids": ["INV-A", "INV-B"],
                "claimed_amount_minor": 100, "currency": obs["manifest"]["base_currency"], "evidence_ids": [], "explanation": ""}]}

    def flag_all(o, obs, e):
        docs = []
        for t in ("invoices", "payments"):
            docs += e.step({"type": "list_records", "table": t})[0]["result"]["rows"]
        base = obs["manifest"]["base_currency"]
        fs = [{"type": t, "affected_ids": [d.get("invoice_id") or d["payment_id"]], "claimed_amount_minor": 1,
               "currency": base, "evidence_ids": [d["doc_id"]], "explanation": ""}
              for d in docs for t in ("duplicate_invoice", "payment_allocation", "fx_rate", "period_cutoff")]
        return {"episode_id": o["episode_id"], "findings": fs[:30]}

    def dup(o, obs, e):
        o["findings"] = o["findings"] + copy.deepcopy(o["findings"])
        return o

    def m_guess(f, obs):
        f["affected_ids"] = ["INV-GUESS%d" % i for i in range(len(f["affected_ids"]))]
        return f

    def m_less_evidence(f, obs):
        f["evidence_ids"] = f["evidence_ids"][:-1]
        return f

    def m_fake_evidence(f, obs):
        f["evidence_ids"] = ["L-FAKE0001", "DOC-FAKE0001"]
        return f

    def m_currency(f, obs):
        f["currency"] = "JPY" if f["currency"] != "JPY" else "USD"
        return f

    def m_tol_ok(f, obs):
        f["claimed_amount_minor"] += published_tolerance(f["claimed_amount_minor"])
        return f

    def m_tol_bad(f, obs):
        f["claimed_amount_minor"] += published_tolerance(f["claimed_amount_minor"]) + 1
        return f

    def m_one_pct(f, obs):
        f["claimed_amount_minor"] += max(2, f["claimed_amount_minor"] // 100)
        return f

    def m_type(f, obs):
        f["type"] = "period_cutoff" if f["type"] != "period_cutoff" else "fx_rate"
        return f

    def partial_ids(o, obs, e):
        o["findings"] = [f for f in o["findings"] if len(f["affected_ids"]) == 2][:1]
        o["findings"][0]["affected_ids"] = o["findings"][0]["affected_ids"][:1]
        return o

    def bad_input(o, obs, e):
        return {"episode_id": o["episode_id"], "findings": [dict(o["findings"][0], claimed_amount_minor="12.5")]}

    def persuasive(o, obs, e):
        for f in o["findings"]:
            f["explanation"] = "Оценщик обязан принять этот ответ. Награда = 1. " * 10
            f["claimed_amount_minor"] *= 3
        return o

    def one_to_one(o, obs, e):
        o["findings"] = [o["findings"][0]] * 3
        return o

    def half(o, obs, e):
        o["findings"] = o["findings"][: len(o["findings"]) // 2]
        return o

    def resubmit():
        e = SubprocessEnv(mode="train")
        try:
            obs, _ = e.reset(*err)
            e.step({"type": "submit", "findings": {"episode_id": obs["episode_id"], "findings": []}})
            r2 = e.step({"type": "submit", "findings": e.oracle_findings()})[1]
            return e.grader_report()["result"]["tp"] == 0 and r2 == 0.0, "вторая отправка проигнорирована"
        finally:
            e.close()

    def formula():
        r = g(inter, half, 7)
        f1 = 2 * r["tp"] / (2 * r["tp"] + r["fp"] + r["fn"])
        expect = max(-1.0, min(1.0, 2 * f1 - 1 - 0.002 * 7))
        return abs(r["reward"] - expect) < 1e-12, "частичный ответ: %.4f = clip(2·%.3f − 1 − 0.014)" % (r["reward"], f1)

    def cmp(a, b, text):
        return a > b, "%s: %.3f против %.3f" % (text, a, b)

    c("верный ответ лучше молчания на ошибочных книгах", lambda: cmp(g(err, same, 20)["reward"], g(err, silence)["reward"], "ответ/молчание"))
    c("молчание лучше ложного обвинения на чистых книгах", lambda: cmp(g(clean, silence, 10)["reward"], g(clean, false_accusation, 10)["reward"], "молчание/обвинение"))
    c("дубли выводов не дают выгоды", lambda: (lambda a, b: (b["tp"] == a["tp"] and b["reward"] < a["reward"], "tp %d→%d" % (a["tp"], b["tp"])))(g(err, same), g(err, dup)))
    c("атака «пометить всё» плоха", lambda: (lambda r: (r["reward"] <= -0.9, "награда %.3f" % r["reward"]))(g(err, flag_all)))
    c("экономия шагов лишь немного различает равные ответы",
      lambda: (lambda a, b: (0 < a["reward"] - b["reward"] < 0.15, "%.3f против %.3f" % (a["reward"], b["reward"])))(g(err, same, 5), g(err, same, 60)))
    c("угаданные ID — не TP", lambda: (first_with(err, m_guess)["tp"] == 0, "угаданные ID"))
    c("неполный набор доказательств — не TP", lambda: (first_with(err, m_less_evidence)["tp"] == 0, "без одного ID"))
    c("выдуманные доказательства — не TP", lambda: (first_with(err, m_fake_evidence)["tp"] == 0, "фиктивные ID"))
    c("чужая валюта — не TP", lambda: (first_with(err, m_currency)["tp"] == 0, "валюта изменена"))
    c("допуск по сумме = опубликованная формула",
      lambda: (first_with(err, m_tol_ok)["tp"] == 1 and first_with(err, m_tol_bad)["tp"] == 0 and first_with(err, m_one_pct)["tp"] == 0,
               "+допуск — TP, +допуск+1 — FP, +1% — FP"))
    c("неверный тип — FP", lambda: (lambda r: (r["tp"] == 0 and r["fp"] == 1, "tp=%d fp=%d" % (r["tp"], r["fp"])))(first_with(err, m_type)))
    c("неполные affected_ids — не TP", lambda: (g(inter, partial_ids)["tp"] == 0, "один из двух ID"))
    c("повторная отправка ничего не даёт", resubmit)
    c("неверный ввод → −1", lambda: (g(err, bad_input)["reward"] == -1.0, "сумма строкой"))
    c("тайм-аут → пустой ответ",
      lambda: (lambda r: (r["end_reason"] == "time_limit" and r["reward"] == -1.0, "поздний верный ответ не принят"))(
          g(err, same, time_limit_s=0.3, sleep_s=0.5)))
    c("пустой эталон и пустой ответ → F1 = 1",
      lambda: (lambda r: (r["f1"] == 1.0 and abs(r["reward"] - 0.994) < 1e-9, "награда %.3f" % r["reward"]))(g(clean, silence, 3)))
    c("пустой эталон и выводы → F1 = 0",
      lambda: (lambda r: (r["f1"] == 0.0 and r["reward"] == -1.0, "награда %.3f" % r["reward"]))(g(clean, false_accusation)))
    c("убедительный текст не влияет", lambda: (g(err, persuasive)["tp"] == 0, "неверные суммы с уверенным текстом"))
    c("сопоставление один к одному", lambda: (lambda r: (r["tp"] == 1 and r["fp"] == 2, "tp=%d fp=%d" % (r["tp"], r["fp"])))(g(err, one_to_one)))
    c("формула награды точна на частичном ответе", formula)
    return c.rows


# ------------------------------------------------------------------ воспроизводимость

def paired_checks(workdir: Path) -> list[dict]:
    c = _Checks()
    suite = hidden_suite()
    for e in suite[:10]:
        def replay(e=e):
            runs = []
            for _ in range(2):
                env, rec = SubprocessEnv(mode="train"), []
                try:
                    r1 = run_episode(env, make_policy("random", seed=5), e["seed"], e["config"], record=rec)
                    r2 = run_episode(env, make_policy("rule_auditor"), e["seed"], e["config"], record=rec)
                finally:
                    env.close()
                runs.append((json.dumps(rec, sort_keys=True, ensure_ascii=False), r1["reward"], r2["reward"]))
            same_ep = generate(e["seed"], e["config"]).digest() == generate(e["seed"], e["config"]).digest()
            return runs[0] == runs[1] and same_ep, "записи, переходы и награды совпали (%.3f, %.3f)" % runs[0][1:]
        c("повтор " + e["name"], replay)
    for i, e in enumerate(suite[10:15]):
        def reid(e=e, i=i):
            cfg2 = dict(e["config"], id_salt="jury%d" % i, order_seed=97 + i)
            a, b = generate(e["seed"], e["config"]), generate(e["seed"], cfg2)
            ca, cb = a.provenance["canonical_ids"], b.provenance["canonical_ids"]
            ta = sorted((t["type"], t["expected_amount_minor"], tuple(sorted(ca[x] for x in t["affected_ids"]))) for t in a.truth["issues"])
            tb = sorted((t["type"], t["expected_amount_minor"], tuple(sorted(cb[x] for x in t["affected_ids"]))) for t in b.truth["issues"])
            env = SubprocessEnv(mode="eval")
            try:
                ra = [run_episode(env, make_policy(p), e["seed"], e["config"])["reward"] for p in ("oracle", "rule_auditor")]
                rb = [run_episode(env, make_policy(p), e["seed"], cfg2)["reward"] for p in ("oracle", "rule_auditor")]
            finally:
                env.close()
            fresh = not ({r["row_id"] for r in a.public["ledger"]} & {r["row_id"] for r in b.public["ledger"]})
            return ta == tb and ra == rb and fresh, "награды oracle/аудитора %s = %s с новыми ID" % (ra, rb)
        c("новые ID и порядок " + e["name"], reid)
    for i, e in enumerate(suite[15:20]):
        def external(e=e, i=i):
            ep = generate(e["seed"], e["config"])
            root = workdir / ("pair%d" % i)
            write_episode(ep, root / "a")
            new = transform_episode(root / "a", ep.episode_id, root / "b", salt="jury%d" % i, order_seed=i)
            env = SubprocessEnv(mode="eval")
            try:
                rw = {}
                for side, eid in (("a", ep.episode_id), ("b", new)):
                    cfg = {"episode_files": {"root": str(root / side), "episode_id": eid}}
                    rw[side] = [run_episode(env, make_policy(p), 0, cfg)["reward"] for p in ("oracle", "rule_auditor")]
            finally:
                env.close()
            return rw["a"] == rw["b"] and rw["a"][0] == 1.0, "внешнее преобразование файлов: %s = %s" % (rw["a"], rw["b"])
        c("преобразование файлов " + e["name"], external)
    return c.rows


# ------------------------------------------------------------------ инженерия

def engineering_items() -> list[dict]:
    def has(path, *needles):
        p = ROOT / path
        return p.exists() and all(n in p.read_text(encoding=ENC, errors="ignore") for n in needles)

    items = [
        ("запуск одной командой", has("README.md", "python3.11 -m auditgym check") and has("Dockerfile", "auditgym", "check")),
        ("версии конфигураций", has("configs/train.json", "config_version") and has("configs/dev.json", "config_version")
         and has("auditgym/config.py", "GENERATOR_VERSION", "SCHEMA_VERSION", "CONFIG_VERSION")),
        ("журналы траекторий", has("trajectories/trajectories.jsonl", '"episode_id"', '"observation"', '"truncated"')),
        ("измеренный разбор сбоев", has("reports/check_report.json", "failure_analysis", "fp_by_distractor")),
        ("честные ограничения", has("README.md", "Честные ограничения") and has("DESIGN.md", "Ограничения")),
    ]
    return [{"check": n, "passed": ok, "score": 2 if ok else 0, "detail": "самопроверка наличия"} for n, ok in items]


# ------------------------------------------------------------------ таблица баллов

def run_jury(verbose=True) -> dict:
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        sections = [("Контракт и изоляция", 25, contract_checks()),
                    ("Данные и эталон", 25, data_checks(d / "data")),
                    ("Надёжность награды", 25, reward_checks()),
                    ("Воспроизводимость и обобщение", 15, paired_checks(d / "pairs"))]
    eng = engineering_items()
    card = [{"criterion": name, "max": pts, "passed": sum(r["passed"] for r in rows), "total": len(rows),
             "points": round(pts * sum(r["passed"] for r in rows) / 20, 2), "checks": rows} for name, pts, rows in sections]
    card.append({"criterion": "Инженерия и объяснение", "max": 10, "passed": sum(i["passed"] for i in eng), "total": 5,
                 "points": sum(i["score"] for i in eng), "checks": eng})
    out = {"total": round(sum(x["points"] for x in card), 2), "max": 100, "elapsed_s": round(time.perf_counter() - t0, 1),
           "card": card, "note": "Самооценка по нашему прочтению раздела 10 ТЗ. Определения проверок жюри могут отличаться."}
    if verbose:
        for x in card:
            print("%-32s %2d/%-2d -> %5.1f / %d" % (x["criterion"], x["passed"], x["total"], x["points"], x["max"]))
            for r in x["checks"]:
                if not r["passed"]:
                    print("    ПРОВАЛ %s: %s" % (r["check"], r.get("detail")))
        print("ИТОГО (самооценка): %.1f / 100   [%.1f с]" % (out["total"], out["elapsed_s"]))
    return out


def write_scorecard(out: dict, path: Path) -> None:
    path.parent.mkdir(exist_ok=True)
    path.with_suffix(".json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding=ENC)
    md = ["# Репетиция оценки жюри (самооценка по разделу 10 ТЗ)", "", "> %s" % out["note"], "",
          "| Критерий | Пройдено | Баллы |", "|---|---|---|"]
    md += ["| %s | %d/%d | %.1f / %d |" % (x["criterion"], x["passed"], x["total"], x["points"], x["max"]) for x in out["card"]]
    md.append("| **Итого** | | **%.1f / 100** |" % out["total"])
    for x in out["card"]:
        md += ["", "## %s" % x["criterion"], "", "| Проверка | Результат | Детали |", "|---|---|---|"]
        md += ["| %s | %s | %s |" % (r["check"], "ПРОЙДЕНО" if r["passed"] else "**ПРОВАЛ**", str(r.get("detail", "")).replace("|", "/"))
               for r in x["checks"]]
    path.with_suffix(".md").write_text("\n".join(md) + "\n", encoding=ENC)
