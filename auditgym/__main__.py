"""Командная строка AuditGym.

python -m auditgym check          единая проверка (офлайн, без зависимостей)
python -m auditgym jury           репетиция оценки жюри по разделу 10 ТЗ
python -m auditgym generate --seed 1 2 --config '{"preset":"mixed"}' --out DIR
python -m auditgym verify DIR     независимая проверка данных и эталона только по файлам
python -m auditgym grade DIR --findings f.json [--steps N]
python -m auditgym pair --seed 1 2 [--config ...]
python -m auditgym transform DIR --episode EP-... --out DIR2
python -m auditgym run --policy rule_auditor --split dev [--mode train|eval]
python -m auditgym demo [--seed N]
python -m auditgym export | trajectories
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

from . import config as C
from .datachecks import check_episode, check_records
from .env import ENV_VERSION, AuditEnv
from .export import ENC, export_split, load_episode_files, write_episode, write_json
from .generator import generate
from .matcher import build_aliases, score
from .policies import BASELINES, RandomExplorer, make_policy
from .runner import ROOT, load_split, run_episode, run_suite, summarize, write_jsonl
from .server import SubprocessEnv
from .transform import transform_episode

try:
    import resource
except ImportError:  # Windows: модуля resource нет, пик памяти не измеряется
    resource = None

FORBIDDEN_KEYS = {"issue_id", "issues", "expected_amount_minor", "evidence_sets", "mutation_id", "mutations",
                  "story_hash", "provenance", "truth", "tolerance_minor", "seed", "ledger_diff", "canonical_ids"}


def _utf8_console():
    """Русский вывод не должен ронять проверку на консоли без UTF-8 (C-локаль в контейнере)."""
    for s in (sys.stdout, sys.stderr):
        try:
            if (s.encoding or "").lower().replace("-", "").replace("_", "") != "utf8":
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ------------------------------------------------------------------ вспомогательное

def versions() -> dict:
    """Версии, от которых зависят эпизоды, переходы и награды (см. README, «Версии»)."""
    return {"env_version": ENV_VERSION, "generator_version": C.GENERATOR_VERSION,
            "schema_version": C.SCHEMA_VERSION, "config_version": C.CONFIG_VERSION}


def git_state() -> dict:
    """Коммит, на котором собраны отчёты, и есть ли незакоммиченные правки. Без git или без .git
    (например, в Docker-образе: .git исключён в .dockerignore) — commit = null."""
    def git(*a):
        return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True, timeout=10)
    try:
        head = git("rev-parse", "HEAD")
        if head.returncode != 0:
            return {"commit": None, "dirty": None}
        dirty = git("status", "--porcelain", "--", ".", ":(exclude)reports")  # отчёты пишет сама проверка
        return {"commit": head.stdout.strip(), "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def peak_rss_mb(who: str):
    """Пик резидентной памяти в МБ: "self" — этот процесс, "children" — самый большой из дочерних.
    ru_maxrss на macOS — в байтах, на Linux — в килобайтах. Без модуля resource (Windows) — None."""
    if resource is None:
        return None
    v = resource.getrusage(resource.RUSAGE_SELF if who == "self" else resource.RUSAGE_CHILDREN).ru_maxrss
    return round(v / (2 ** 20 if sys.platform == "darwin" else 2 ** 10), 1)


def _keys(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k)
            _keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, acc)
    return acc


def leak_scan(rows, truths) -> list[str]:
    """В наблюдениях не должно быть закрытых ключей и issue_id."""
    issue_ids = {i["issue_id"] for t in truths for i in t["issues"]}
    problems = []
    for row in rows:
        obs = row["observation"]
        bad = _keys(obs, set()) & FORBIDDEN_KEYS
        if bad:
            problems.append("эпизод %s шаг %s: закрытые ключи %s" % (row["episode_id"], row["step"], sorted(bad)))
        text = json.dumps(obs, ensure_ascii=False)
        if any(i in text for i in issue_ids):
            problems.append("эпизод %s шаг %s: утечка issue_id" % (row["episode_id"], row["step"]))
    return problems


def truth_signature(ep) -> list:
    """Эталон с публичными ID, заменёнными на канонические сущности истории."""
    canon = ep.provenance["canonical_ids"]
    cid = lambda x: x if x.startswith("FX-") else canon[x]
    return sorted((t["type"], t["expected_amount_minor"], t["currency"], t["tolerance_minor"],
                   tuple(sorted(canon[a] for a in t["affected_ids"])),
                   tuple(sorted(tuple(sorted(cid(e) for e in es)) for es in t["evidence_sets"]))) for t in ep.truth["issues"])


def public_signature(ep) -> dict:
    """Открытые таблицы с каноническими ID и без учёта порядка."""
    canon = ep.provenance["canonical_ids"]

    def c(v):
        if isinstance(v, str):
            return canon.get(v, v)
        if isinstance(v, list):
            return [c(x) for x in v]
        if isinstance(v, dict):
            return {k: c(x) for k, x in v.items()}
        return v
    return {t: sorted(json.dumps(c(r), sort_keys=True, ensure_ascii=False) for r in ep.public[t])
            for t in ("ledger", "invoices", "payments", "fx")}


def paired_checks(entries, workdir: Path) -> list[dict]:
    out = []
    for n, e in enumerate(entries):
        # (а) точный повтор: те же seed, конфиг, версия и действия → те же записи, переходы, награды
        trajs, rewards = [], []
        for _ in range(2):
            rec, env = [], AuditEnv(mode="train")
            r1 = run_episode(env, make_policy("random", seed=7), e["seed"], e["config"], record=rec)
            r2 = run_episode(env, make_policy("rule_auditor"), e["seed"], e["config"], record=rec)
            trajs.append(json.dumps(rec, sort_keys=True, ensure_ascii=False))
            rewards.append((r1["reward"], r2["reward"]))
        ok = trajs[0] == trajs[1] and rewards[0] == rewards[1] and \
            generate(e["seed"], e["config"]).digest() == generate(e["seed"], e["config"]).digest()
        out.append({"check": "повтор", "name": e["name"], "passed": ok})
        # (б) новые ID и порядок через конфиг: эталон и все таблицы совпадают с точностью до ID
        cfg2 = dict(e["config"], id_salt="paired-%d" % n, order_seed=1000 + n)
        a, b = generate(e["seed"], e["config"]), generate(e["seed"], cfg2)
        env = AuditEnv(mode="eval")
        ra = [run_episode(env, make_policy(p), e["seed"], e["config"])["reward"] for p in ("oracle", "rule_auditor")]
        rb = [run_episode(env, make_policy(p), e["seed"], cfg2)["reward"] for p in ("oracle", "rule_auditor")]
        ok = (truth_signature(a) == truth_signature(b) and public_signature(a) == public_signature(b)
              and not ({i["invoice_id"] for i in a.public["invoices"]} & {i["invoice_id"] for i in b.public["invoices"]})
              and ra == rb and ra[0] == 1.0 and not check_episode(b))
        out.append({"check": "новые ID и порядок", "name": e["name"], "passed": ok, "rewards": rb})
        # (в) внешнее преобразование файлов
        root = workdir / ("pair%d" % n)
        write_episode(a, root / "a")
        new = transform_episode(root / "a", a.episode_id, root / "b", salt="ext%d" % n, order_seed=n)
        env = AuditEnv(mode="eval")
        rw = {}
        for side, eid in (("a", a.episode_id), ("b", new)):
            cfg = {"episode_files": {"root": str(root / side), "episode_id": eid}}
            rw[side] = [run_episode(env, make_policy(p), 0, cfg)["reward"] for p in ("oracle", "rule_auditor", "reconciler")]
        out.append({"check": "преобразование файлов", "name": e["name"], "passed": rw["a"] == rw["b"] and rw["a"][0] == 1.0,
                    "rewards": rw})
    return out


def failure_analysis(entries, results, trajectories) -> dict:
    """Измеренный разбор ошибок базовых политик: по типам ошибок и по видам отвлекающих случаев."""
    eps = {}
    for e in entries:
        ep = generate(e["seed"], e["config"])
        eps[ep.episode_id] = ep
    last = {}
    for row in trajectories:
        if row["action"] and isinstance(row["action"], dict) and row["action"].get("type") == "submit":
            last[(row["episode_id"], row["policy"])] = row["action"]["findings"]
    out = {}
    for r in results:
        ep = eps.get(r["episode_id"])
        if ep is None:
            continue
        pol = out.setdefault(r["policy"], {"fn_by_type": Counter(), "fn_in_interaction": 0,
                                           "fp_by_type": Counter(), "fp_by_distractor": Counter()})
        by_id = {i["issue_id"]: i for i in ep.truth["issues"]}
        inter = {m["mutation_id"] for m in ep.provenance["mutations"] if m.get("issue_type") == "interaction"}
        for iid in r["missed_issues"]:
            pol["fn_by_type"][by_id[iid]["type"]] += 1
            pol["fn_in_interaction"] += by_id[iid]["mutation_id"] in inter
        fs = (last.get((r["episode_id"], r["policy"])) or {}).get("findings") or []
        tag_of = {x: k for k, ids in ep.provenance["distractors"].items() for x in ids}
        truth_ids = {a for i in ep.truth["issues"] for a in i["affected_ids"]}
        for idx in r["unmatched_findings"]:
            if idx >= len(fs):
                continue
            f = fs[idx]
            pol["fp_by_type"][f.get("type")] += 1
            tags = {tag_of[a] for a in f.get("affected_ids", []) if a in tag_of}
            for t in tags or ():
                pol["fp_by_distractor"][t] += 1
            if not tags:
                key = "real_issue_wrong_fields" if set(f.get("affected_ids", [])) & truth_ids else "ordinary_record"
                pol["fp_by_distractor"][key] += 1
    return {k: {kk: (dict(vv) if isinstance(vv, Counter) else vv) for kk, vv in v.items()} for k, v in out.items()}


class StepLimitExplorer(RandomExplorer):
    """Случайный исследователь, который НЕ отправляет ответ: эпизод заканчивается по лимиту
    100 шагов (truncated=true, пустой ответ). Нужен журналу траекторий как пример усечения."""

    name = "random_no_submit"

    def begin(self, obs):
        super().begin(obs)
        self.budget = float("inf")

    def act(self, obs):
        # RandomExplorer отправляет ответ, когда шагов почти не осталось; здесь — никогда
        return super().act(dict(obs, steps_remaining=max(2, obs.get("steps_remaining", 2))))


def _traj_policy(name, seed=0):
    return StepLimitExplorer(seed) if name == StepLimitExplorer.name else make_policy(name, seed)


# Журнал траекторий: (режим, сколько первых эпизодов split, политики). Прогоны oracle не публикуются:
# их действия построены из эталона.
TRAJECTORY_PLAN = (
    ("train", None, ("random", "reconciler", "flag_all", "rule_auditor")),
    ("train", 3, (StepLimitExplorer.name,)),                     # конец по лимиту шагов: truncated=true
    ("eval", 3, ("rule_auditor", "reconciler", StepLimitExplorer.name)),  # eval: награда 0, info пуст
)


def trajectory_rows() -> list[dict]:
    rows = []
    for split in ("train", "dev"):
        entries = load_split(split)
        for mode, n, policies in TRAJECTORY_PLAN:
            with SubprocessEnv(mode=mode) as env:
                for e in entries[:n]:
                    for name in policies:
                        rec = []
                        run_episode(env, _traj_policy(name), e["seed"], e["config"], split, rec)
                        for r in rec:
                            r.update(mode=mode, env_version=ENV_VERSION, generator_version=C.GENERATOR_VERSION)
                        rows += rec
    return rows


def published_trajectories_problems(truths) -> tuple[list[str], dict]:
    """Опубликованный trajectories.jsonl: нет закрытых данных в наблюдениях, версия среды текущая."""
    path = ROOT / "trajectories" / "trajectories.jsonl"
    if not path.exists():
        return ["нет trajectories/trajectories.jsonl"], {}
    with path.open(encoding=ENC) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    probs = leak_scan(rows, truths)
    stale = sorted({str(r.get("env_version")) for r in rows} - {ENV_VERSION})
    if stale:
        probs.append("trajectories.jsonl собран другой версией среды (%s ≠ %s): пересоберите командой trajectories"
                     % (", ".join(stale), ENV_VERSION))
    runs = {(r["episode_id"], r["policy"], r.get("mode")) for r in rows}
    stat = {"rows": len(rows), "runs": len(runs),
            "runs_truncated": len({(r["episode_id"], r["policy"], r.get("mode")) for r in rows if r["truncated"]}),
            "runs_eval": len({k for k in runs if k[2] == "eval"})}
    return probs, stat


def _table(summary) -> str:
    cols = ["политика", "P", "R", "F1", "возврат", "эксп. возврат", "шаги", "время, с", "TP", "FP", "FN"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for name, s in summary.items():
        lines.append("| %s | %.3f | %.3f | %.3f | %.3f | %.3f | %.1f | %.3f | %d | %d | %d |" % (
            name, s["precision"], s["recall"], s["f1"], s["return"], s["experimental_return"], s["steps"], s["time_s"],
            s["tp"], s["fp"], s["fn"]))
    return "\n".join(lines)


def _stress_entries(n):
    rnd = random.Random(20261119)
    out = []
    for i in range(n):
        errs = {t: rnd.randint(0, 4) for t in C.ERROR_TYPES}
        inter = {t: rnd.randint(0, 2) for t in C.INTERACTIONS}
        while sum(errs.values()) + 2 * sum(inter.values()) > C.MAX_ISSUES:
            k = rnd.choice(C.ERROR_TYPES)
            errs[k] = max(0, errs[k] - 1)
            if not sum(errs.values()):
                inter = {t: 0 for t in C.INTERACTIONS}
        cfg = {"errors": errs, "interactions": inter, "id_salt": "st%d" % i, "order_seed": i}
        if i % 2:  # каждый второй — со случайными размерами, долей валют и интенсивностью ловушек
            cfg.update({"n_invoices": sorted([rnd.randint(20, 120), rnd.randint(20, 120)]),
                        "n_customers": sorted([rnd.randint(3, 40), rnd.randint(3, 40)]),
                        "background_entries": sorted([rnd.randint(0, 200), rnd.randint(0, 200)]),
                        "foreign_share": rnd.choice([0, 0.1, 0.5, 1]),
                        "distractors": {k: rnd.randint(0, 5) for k in C.DISTRACTORS}})
        out.append({"name": "стресс%d" % i, "seed": 50000 + i, "config": cfg})
    return out


# ------------------------------------------------------------------ команды

def cmd_check(args) -> int:
    t_start = time.perf_counter()
    report = {**versions(), "git": git_state(), "python": sys.version.split()[0], "platform": sys.platform,
              "sections": {}}
    ok_all = True

    def section(name, passed, detail):
        nonlocal ok_all
        ok_all &= bool(passed)
        report["sections"][name] = {"passed": bool(passed), **detail}
        short = {k: v for k, v in detail.items() if not isinstance(v, (list, dict))}
        print("[%s] %s %s" % ("ПРОЙДЕНО" if passed else "ПРОВАЛ", name, json.dumps(short, ensure_ascii=False)))

    # 1. тесты
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    with open(Path(tempfile.gettempdir()) / "auditgym_unittest.log", "w", encoding=ENC) as log:
        res = unittest.TextTestRunner(verbosity=0, stream=sys.stderr if args.verbose else log).run(suite)
    fails = [str(t[0]) for t in res.failures + res.errors]
    section("unit_tests", res.wasSuccessful(), {"run": res.testsRun, "failed": len(fails), "skipped": len(res.skipped),
                                                "failures": fails})
    from . import landlock
    from .adapters import policy_sandbox
    sandbox = policy_sandbox()
    report["policy_sandbox"] = sandbox + ("-abi%d" % landlock.abi()[0] if sandbox == "landlock" else "")
    print("[ИНФО] песочница процесса политики: %s" % report["policy_sandbox"])

    train, dev = load_split("train"), load_split("dev")
    # 2. детерминизм и опубликованные эпизоды
    det = []
    for split, entries in (("train", train), ("dev", dev)):
        idx_path = ROOT / "episodes" / split / "index.json"
        committed = {x["seed"]: x["digest"] for x in json.loads(idx_path.read_text(encoding=ENC))} if idx_path.exists() else {}
        for e in entries:
            d1, d2 = generate(e["seed"], e["config"]).digest(), generate(e["seed"], e["config"]).digest()
            if d1 != d2:
                det.append("%s/%s: недетерминирован" % (split, e["name"]))
            if committed and committed.get(e["seed"]) != d1:
                det.append("%s/%s: отличается от опубликованного эпизода" % (split, e["name"]))
    section("determinism", not det, {"episodes": len(train) + len(dev), "problems": det})

    # 3. данные и эталон: train, dev и случайные (в т.ч. экстремальные) конфигурации
    probs, sizes = [], []
    for e in train + dev + _stress_entries(args.stress):
        try:
            ep = generate(e["seed"], e["config"])
        except Exception as ex:  # noqa
            probs.append("%s: генерация не удалась: %r" % (e["name"], ex))
            continue
        c = ep.public["manifest"]["counts"]
        sizes.append((c["ledger"], c["journal_entries"], c["documents"], len(ep.truth["issues"])))
        probs += ["%s: %s" % (e["name"], p) for p in check_episode(ep)]
    rng_ = lambda i: [min(s[i] for s in sizes), max(s[i] for s in sizes)]
    section("data_and_truth", not probs, {"episodes": len(sizes), "problems": probs[:50], "ledger_rows": rng_(0),
                                          "journal_entries": rng_(1), "documents": rng_(2), "issues": rng_(3)})

    # 4. базовые политики в режиме eval в отдельном процессе
    traj, results = [], {}
    for split, entries in (("train", train), ("dev", dev)):
        rec = []
        results[split] = run_suite(entries, list(BASELINES) + ["rule_auditor"], mode="eval", isolated=True, split=split, record=rec)
        traj += rec
    summ = {s: summarize(r) for s, r in results.items()}
    expect = []
    for s, sm in summ.items():
        if sm["oracle"]["return"] != 1.0:
            expect.append("%s: возврат oracle %.3f ≠ 1" % (s, sm["oracle"]["return"]))
        if not sm["flag_all"]["return"] < sm["reconciler"]["return"] < sm["rule_auditor"]["return"] <= sm["oracle"]["return"]:
            expect.append("%s: ожидалось flag_all < reconciler < rule_auditor ≤ oracle" % s)
        if sm["random"]["return"] >= 0:
            expect.append("%s: случайная политика не должна получать положительный возврат" % s)
    section("baselines", not expect, {"summary": summ, "problems": expect})

    # 5. утечки в записанных траекториях
    truths = [generate(e["seed"], e["config"]).truth for e in train + dev]
    leaks = leak_scan(traj, truths)
    pub_probs, pub_stat = published_trajectories_problems(truths)  # и опубликованный журнал траекторий
    section("no_leakage", not leaks and not pub_probs,
            {"observations_scanned": len(traj), "published_trajectories": pub_stat, "problems": (leaks + pub_probs)[:20]})

    # 6. парные проверки
    with tempfile.TemporaryDirectory() as d:
        pc = paired_checks(dev, Path(d))
    section("paired_checks", all(x["passed"] for x in pc), {"total": len(pc), "passed": sum(x["passed"] for x in pc), "checks": pc})

    # 7. разбор сбоев (диагностика) — до репетиции жюри, которая его читает
    report["failure_analysis"] = {s: failure_analysis(e, results[s], [t for t in traj if t["split"] == s])
                                  for s, e in (("train", train), ("dev", dev))}
    all_res = [r for rs in results.values() for r in rs]
    report["resources"] = {
        "max_episode_time_s": max(r["time_s"] for r in all_res),
        "max_steps_used": max(r["steps"] for r in all_res),
        "peak_rss_mb_orchestrator": peak_rss_mb("self"),
        "peak_rss_mb_env_process": peak_rss_mb("children"),  # самый большой из дочерних процессов
        "limits": {"time_per_episode_s": 60, "steps": 100, "ram_gb": 4},
    }
    if resource is None:
        report["resources"]["peak_rss_note"] = "измерение памяти на %s недоступно: нет модуля resource" % sys.platform
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    write_json(out / "check_report.json", report)

    # 8. репетиция оценки жюри (чёрный ящик, свежие «скрытые» seed)
    from .jury import run_jury, write_scorecard
    jr = run_jury(verbose=False)
    write_scorecard(jr, out / "jury_scorecard")
    failed = ["%s: %s" % (x["criterion"], r["check"]) for x in jr["card"] for r in x["checks"] if not r["passed"]]
    section("jury_rehearsal", jr["total"] == 100, {"score": jr["total"], "max": 100, "failed": failed,
                                                   "card": {x["criterion"]: "%d/%d" % (x["passed"], x["total"]) for x in jr["card"]}})
    print("[ИНФО] ресурсы %s" % json.dumps(report["resources"], ensure_ascii=False))

    report["elapsed_s"] = round(time.perf_counter() - t_start, 1)
    report["passed"] = ok_all
    write_json(out / "check_report.json", report)
    g = report["git"]
    md = ["# Отчёт проверки", "", "среда %s, генератор %s, схемы %s, конфиг %s; коммит %s%s; python %s (%s), %.1f с" % (
        ENV_VERSION, C.GENERATOR_VERSION, C.SCHEMA_VERSION, C.CONFIG_VERSION, (g["commit"] or "неизвестен")[:12],
        " + незакоммиченные правки" if g["dirty"] else "", report["python"], report["platform"], report["elapsed_s"]), ""]
    md += ["- **%s**: %s" % (name, "ПРОЙДЕНО" if sec["passed"] else "ПРОВАЛ") for name, sec in report["sections"].items()]
    for s in ("train", "dev"):
        md += ["", "## Базовые политики на %s (режим eval, отдельный процесс)" % s, "", _table(summ[s])]
    md += ["", "## Ресурсы", "", "```json", json.dumps(report["resources"], indent=2, ensure_ascii=False), "```",
           "", "## Разбор сбоев (dev)", "", "```json", json.dumps(report["failure_analysis"]["dev"], indent=2, ensure_ascii=False), "```"]
    (out / "check_report.md").write_text("\n".join(md) + "\n", encoding=ENC)
    print()
    print(_table(summ["dev"]))
    print("\n%s за %.1f с → reports/check_report.md, reports/jury_scorecard.md" % (
        "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ" if ok_all else "ЕСТЬ ПРОВАЛЫ", report["elapsed_s"]))
    return 0 if ok_all else 1


def cmd_jury(args) -> int:
    from .jury import run_jury, write_scorecard
    out = run_jury()
    write_scorecard(out, ROOT / "reports" / "jury_scorecard")
    print("→ reports/jury_scorecard.md")
    return 0 if out["total"] == 100 else 1


def cmd_export(args) -> int:
    for split in ("train", "dev"):
        idx = export_split(load_split(split), split, ROOT / "episodes")
        print("записано эпизодов %s: %d → episodes/%s" % (split, len(idx), split))
    return 0


def cmd_trajectories(args) -> int:
    rows = trajectory_rows()
    write_jsonl(ROOT / "trajectories" / "trajectories.jsonl", rows)
    runs = {(r["episode_id"], r["policy"], r["mode"]) for r in rows}
    print("записано шагов: %d, прогонов: %d (усечённых: %d, в режиме eval: %d) → trajectories/trajectories.jsonl" % (
        len(rows), len(runs), len({(r["episode_id"], r["policy"], r["mode"]) for r in rows if r["truncated"]}),
        sum(1 for k in runs if k[2] == "eval")))
    return 0


def cmd_run(args) -> int:
    rec = [] if args.trajectories else None
    res = run_suite(load_split(args.split), [args.policy], mode=args.mode, isolated=not args.in_process,
                    split=args.split, record=rec, seed=args.seed)
    for r in res:
        print("%-14s %-13s награда=%+.3f P=%.2f R=%.2f F1=%.2f шагов=%d" % (
            r["episode_id"], r["policy"], r["reward"], r["precision"], r["recall"], r["f1"], r["steps"]))
    print(json.dumps(summarize(res), indent=2, ensure_ascii=False))
    if rec is not None:
        write_jsonl(Path(args.trajectories), rec)
    return 0


def cmd_demo(args) -> int:
    with SubprocessEnv(mode="train") as env:
        obs, info = env.reset(args.seed, {"preset": args.preset})
        m = obs["manifest"]
        print("эпизод %s | %s | база %s | пояс %s | период %s … %s" % (
            info["episode_id"], m["company_name"], m["base_currency"], m["timezone"], m["period_start"], m["period_end"]))
        print("записи:", json.dumps(m["counts"], ensure_ascii=False))
        pol = make_policy(args.policy)
        pol.begin(obs)
        while True:
            a = pol.act(obs)
            obs, r, te, tr, inf = env.step(a)
            if a["type"] == "submit":
                print("шаг %3d submit: %d выводов" % (obs["step"], len(a["findings"]["findings"])))
                for f in a["findings"]["findings"]:
                    print("        %-18s %-28s %12d %s  доказательства=%s" % (f["type"], ",".join(f["affected_ids"]),
                          f["claimed_amount_minor"], f["currency"], ",".join(f["evidence_ids"])))
            else:
                res = obs.get("result") or {}
                print("шаг %3d %-12s %-9s строк=%-3s далее=%s" % (obs["step"], a["type"], a.get("table", ""),
                      len(res.get("rows", [])), res.get("next_cursor")))
            if te or tr:
                print("terminated=%s truncated=%s награда=%+.3f info=%s" % (te, tr, r, json.dumps(inf, ensure_ascii=False)))
                break
        rep = env.grader_report()["result"]
        print("оценщик: P=%.2f R=%.2f F1=%.2f награда=%+.3f" % (rep["precision"], rep["recall"], rep["f1"], rep["reward"]))
    return 0


def _load_config(text):
    if text is None:
        return {}
    p = Path(text)
    return json.loads(p.read_text(encoding=ENC)) if p.exists() else json.loads(text)


def cmd_generate(args) -> int:
    cfg = _load_config(args.config)
    if args.id_salt is not None:
        cfg["id_salt"] = args.id_salt
    if args.order_seed is not None:
        cfg["order_seed"] = args.order_seed
    out, index = Path(args.out), []
    for seed in args.seed:
        ep = generate(seed, cfg)
        write_episode(ep, out)
        index.append({"seed": seed, "episode_id": ep.episode_id, "digest": ep.digest(), "n_issues": len(ep.truth["issues"]),
                      "counts": ep.public["manifest"]["counts"]})
        print("%s seed=%d ошибок=%d %s" % (ep.episode_id, seed, len(ep.truth["issues"]), json.dumps(ep.public["manifest"]["counts"])))
    write_json(out / "index.json", index)
    return 0


def cmd_verify(args) -> int:
    root = Path(args.root)
    ids = sorted(p.name for p in (root / "public").iterdir() if p.is_dir()) if (root / "public").exists() else []
    bad = 0
    for eid in ids:
        try:
            problems = check_records(*load_episode_files(root, eid))
        except (OSError, ValueError, KeyError) as e:
            problems = ["не удалось прочитать файлы эпизода: %s" % e]
        bad += bool(problems)
        truth = json.loads((root / "grader" / eid / "truth.json").read_text(encoding=ENC)) if not problems else None
        print("[%s] %s%s" % ("ПРОЙДЕНО" if not problems else "ПРОВАЛ", eid,
                             " ошибок=%d" % len(truth["issues"]) if truth else " " + "; ".join(problems[:5])))
    print("проверено эпизодов: %d из %d" % (len(ids) - bad, len(ids)))
    return 0 if ids and not bad else 1


def cmd_grade(args) -> int:
    root = Path(args.root)
    payload = json.loads(Path(args.findings).read_text(encoding=ENC))
    pub, _, truth, _ = load_episode_files(root, args.episode or payload.get("episode_id"))
    print(json.dumps(score(payload, truth, build_aliases(pub), args.steps), indent=2, ensure_ascii=False))
    return 0


def cmd_pair(args) -> int:
    cfg = _load_config(args.config)
    entries = [{"name": "seed%d" % s, "seed": s, "config": cfg} for s in args.seed]
    with tempfile.TemporaryDirectory() as d:
        res = paired_checks(entries, Path(d))
    for r in res:
        print("[%s] %-22s %s" % ("ПРОЙДЕНО" if r["passed"] else "ПРОВАЛ", r["check"], r["name"]))
    print("парных проверок пройдено: %d из %d" % (sum(r["passed"] for r in res), len(res)))
    return 0 if all(r["passed"] for r in res) else 1


def cmd_transform(args) -> int:
    print(transform_episode(Path(args.root), args.episode, Path(args.out), salt=args.salt, order_seed=args.order_seed))
    return 0


def main(argv=None) -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(prog="python -m auditgym", description="AuditGym — RL-среда для ИИ-аудитора")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="единая офлайн-проверка всего")
    c.add_argument("--stress", type=int, default=200, help="число случайных эпизодов для проверки данных")
    c.add_argument("--verbose", action="store_true")
    sub.add_parser("jury", help="репетиция оценки жюри по разделу 10 (самооценка)")
    sub.add_parser("export", help="пересобрать episodes/ (train и dev)")
    sub.add_parser("trajectories", help="пересобрать trajectories/trajectories.jsonl")
    r = sub.add_parser("run", help="прогнать политику на train/dev")
    r.add_argument("--policy", default="rule_auditor", choices=list(BASELINES) + ["rule_auditor"])
    r.add_argument("--split", default="dev", choices=["train", "dev"])
    r.add_argument("--mode", default="eval", choices=["train", "eval"])
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--in-process", action="store_true")
    r.add_argument("--trajectories", default=None)
    d = sub.add_parser("demo", help="пошаговый показ работы среды")
    d.add_argument("--seed", type=int, default=42)
    d.add_argument("--preset", default="interacting")
    d.add_argument("--policy", default="rule_auditor", choices=["random", "reconciler", "flag_all", "rule_auditor"])
    g = sub.add_parser("generate", help="эпизоды для любых seed и конфигураций → OUT/public, OUT/grader")
    g.add_argument("--seed", type=int, nargs="+", required=True)
    g.add_argument("--config", default=None, help="JSON-файл или JSON-строка")
    g.add_argument("--id-salt", default=None)
    g.add_argument("--order-seed", type=int, default=None)
    g.add_argument("--out", required=True)
    v = sub.add_parser("verify", help="независимая проверка эпизодов только по файлам")
    v.add_argument("root", help="каталог с public/ и grader/ (например, episodes/dev)")
    gr = sub.add_parser("grade", help="оценить файл findings.json общим matcher'ом")
    gr.add_argument("root")
    gr.add_argument("--findings", required=True)
    gr.add_argument("--episode", default=None)
    gr.add_argument("--steps", type=int, default=0, help="число шагов исследования для штрафа")
    pa = sub.add_parser("pair", help="парные проверки для заданных seed")
    pa.add_argument("--seed", type=int, nargs="+", required=True)
    pa.add_argument("--config", default=None)
    tf = sub.add_parser("transform", help="переименовать все ID и перемешать строки экспортированного эпизода")
    tf.add_argument("root")
    tf.add_argument("--episode", required=True)
    tf.add_argument("--out", required=True)
    tf.add_argument("--salt", default="t1")
    tf.add_argument("--order-seed", type=int, default=1)
    a = ap.parse_args(argv)
    return {"check": cmd_check, "jury": cmd_jury, "export": cmd_export, "trajectories": cmd_trajectories, "run": cmd_run,
            "demo": cmd_demo, "generate": cmd_generate, "verify": cmd_verify, "grade": cmd_grade, "pair": cmd_pair,
            "transform": cmd_transform}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
