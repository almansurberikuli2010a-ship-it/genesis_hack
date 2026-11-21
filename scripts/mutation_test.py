"""Мутационный тест самой проверки: по одному вносим НАСТОЯЩИЙ баг во временную копию
проекта и убеждаемся, что `python -m auditgym jury` снимает баллы в нужном критерии.
Проверка, которая не падает на заведомо сломанной среде, ничего не стоит.

    python3 scripts/mutation_test.py                  # все мутанты (~2–3 мин), Python ≥ 3.11
    python3 scripts/mutation_test.py rounding_broken  # один мутант
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent

# имя: (файл, исходный фрагмент, испорченный фрагмент, что это за баг)
MUTANTS = {
    "evidence_ignored": ("auditgym/matcher.py",
                         'return any(frozenset(es) <= nf["evidence"] for es in issue["evidence_sets"])', "return True",
                         "matcher не проверяет доказательства"),
    "page_limit_broken": ("auditgym/env.py", "limit = min(limit, PAGE_MAX)", "limit = limit",
                          "страница больше 50 строк"),
    "truth_leak_in_info": ("auditgym/env.py",
                           'return obs, {"episode_id": ep.episode_id, "mode": self.mode, "env_version": ENV_VERSION}',
                           'return obs, {"episode_id": ep.episode_id, "mode": self.mode, "env_version": ENV_VERSION, "issues": ep.truth["issues"]}',
                           "эталон в info после reset"),
    "rounding_broken": ("auditgym/money.py", "return int(x.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))", "return int(x)",
                        "усечение вместо банковского округления"),
    "local_date_utc": ("auditgym/money.py", "return dt.astimezone(tz_from_offset(company_offset)).date()",
                       'return dt.astimezone(tz_from_offset("+00:00")).date()',
                       "дата курса по UTC вместо часового пояса компании"),
    "tolerance_10pct": ("auditgym/matcher.py", 'if abs(nf["amount"] - issue["expected_amount_minor"]) > tolerance(issue, ctx):',
                        'if abs(nf["amount"] - issue["expected_amount_minor"]) > abs(issue["expected_amount_minor"]) // 10:',
                        "допуск 10% вместо опубликованного τ = 0"),
    "nondeterministic_story": ("auditgym/story.py", 'self.rng = rng("story", self.story_hash)',
                               'import os; self.rng = rng("story", self.story_hash, os.urandom(4).hex())',
                               "недетерминированный генератор"),
    "no_one_to_one": ("auditgym/matcher.py", '"fp": len(findings) - len(pairs),', '"fp": sum(1 for a in adj if not a),',
                      "сопоставление без «один к одному»: повторы верного вывода не считаются FP"),
    "eval_leaks_reward": ("auditgym/env.py", 'reward = res["reward"] if self.mode == "train" else 0.0\n        # ответ о структуре',
                          'reward = res["reward"]\n        # ответ о структуре', "награда видна политике в режиме eval"),
    "provenance_drops_changes": ("auditgym/render.py", "if rid not in after or after[rid][1] != rec:", "if rid not in after:",
                                 "provenance теряет изменённые строки (книги до ошибок не восстановить)"),
    "off_policy_posting": ("auditgym/posting.py", "            susp = cash\n", "            susp = cash - 1\n",
                           "платёж без расписки проведён не по политике (на 1 минимальную единицу)"),
}


def main(names):
    names = names or list(MUTANTS)
    caught = 0
    for name in names:
        f, a, b, what = MUTANTS[name]
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "auditgym"
            shutil.copytree(SRC, d, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            p = d / f
            s = p.read_text(encoding="utf-8")
            assert a in s, (name, a)
            p.write_text(s.replace(a, b, 1), encoding="utf-8")
            out = subprocess.run([sys.executable, "-m", "auditgym", "jury"], cwd=d, capture_output=True, text=True,
                                 encoding="utf-8", timeout=1200).stdout
        lost = [l.split("->")[0].strip() for l in out.splitlines() if "->" in l and "/20" in l and "20/20" not in l]
        total = next((l.split(":")[1].split("[")[0].strip() for l in out.splitlines() if "ИТОГО" in l), "?")
        caught += bool(lost)
        print("[%s] %-26s %-62s | %s | %s" % ("ПОЙМАН" if lost else "ПРОПУЩЕН", name, what, "; ".join(lost) or "—", total))
    print("поймано мутантов: %d из %d" % (caught, len(names)))
    return 0 if caught == len(names) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
