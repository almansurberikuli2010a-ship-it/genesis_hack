"""Отдельный процесс оценщика, калькулятор-песочница, базовые политики, адаптеры, эпизоды из файлов."""
import json
import tempfile
import unittest
from pathlib import Path

import auditgym
from auditgym import PRESETS, ConfigError, generate, make_env
from auditgym.adapters import BASELINE_ADAPTERS, run_policy
from auditgym.calculator import CalcError, evaluate
from auditgym.datachecks import check_episode, check_records
from auditgym.env import AuditEnv
from auditgym.export import load_episode_files, write_episode
from auditgym.policies import make_policy
from auditgym.runner import run_episode
from auditgym.server import RemoteError, SubprocessEnv
from auditgym.transform import transform_episode
from tests.helpers import ERRS, oracle_findings


def silent(obs):
    return {"type": "submit", "findings": {"episode_id": obs["episode_id"], "findings": []}}


class Isolation(unittest.TestCase):
    def test_subprocess_contract_and_token(self):
        with SubprocessEnv(mode="eval") as env:
            env.reset(5, ERRS)
            o, r, te, tr, inf = env.step({"type": "list_records", "table": "ledger"})
            self.assertEqual(len(o["result"]["rows"]), 50)
            for req in ({"cmd": "grader_report", "token": "guess"}, {"cmd": "oracle"},
                        {"cmd": "reset", "seed": 1, "config": {"bad": 1}}):
                with self.assertRaises(RemoteError):
                    env._call(req)
            self.assertEqual(env._call({"cmd": "step"})["obs"]["error"]["code"], "unknown_action")
        with SubprocessEnv(mode="eval") as env:
            with self.assertRaises(RemoteError) as ctx:
                env.step({"type": "calculator", "expression": "1"})  # step до reset
            self.assertEqual(str(ctx.exception), "internal error")
            env.reset(5, ERRS)
            self.assertIsNone(env.grader_report()["result"])

    def test_same_results_in_and_out_of_process(self):
        a = run_episode(AuditEnv(mode="eval"), make_policy("rule_auditor"), 5, ERRS)
        with SubprocessEnv(mode="eval") as env:
            b = run_episode(env, make_policy("rule_auditor"), 5, ERRS)
        for k in ("reward", "f1", "tp", "fp", "fn", "steps"):
            self.assertEqual(a[k], b[k])


class Calculator(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(evaluate("0.1 + 0.2"), "0.3")
        self.assertEqual(evaluate("125000 * 1.0823 / 100"), "1352.875")
        self.assertEqual(evaluate("-(3 - 5) * 2"), "4")
        self.assertEqual(evaluate("7 // 2 + 7 % 2"), "4")

    def test_rejects_code_and_files(self):
        for e in ("__import__('os')", "open('truth.json')", "2**999999", "a+1", "[1,2]", "1 if 1 else 2", "lambda: 1",
                  "1e400", "'1'+'2'", "1/0", "(" * 200 + "1" + ")" * 200, "9" * 400, 5, None):
            with self.assertRaises(CalcError, msg=repr(e)):
                evaluate(e)


class Baselines(unittest.TestCase):
    def test_ordering(self):
        for cfg in (ERRS, {}):
            env = AuditEnv(mode="eval")
            r = {p: run_episode(env, make_policy(p), 14, cfg)["reward"] for p in ("oracle", "rule_auditor", "flag_all", "random")}
            self.assertEqual(r["oracle"], 1.0)
            self.assertGreater(r["rule_auditor"], 0.9)
            self.assertLess(r["flag_all"], 0)
            if cfg:  # на чистых книгах молчание случайной политики может сравняться с аудитором
                self.assertGreater(r["rule_auditor"], r["random"])
            else:
                self.assertGreaterEqual(r["rule_auditor"], r["random"])

    def test_random_explorer_is_seeded(self):
        a = run_episode(AuditEnv(), make_policy("random", seed=3), 9, ERRS)
        b = run_episode(AuditEnv(), make_policy("random", seed=3), 9, ERRS)
        self.assertEqual(a, dict(b, time_s=a["time_s"]))


class Api(unittest.TestCase):
    def test_exports_and_presets(self):
        for name in ("AuditEnv", "SubprocessEnv", "make_env", "run_policy", "generate", "score", "PRESETS"):
            self.assertTrue(hasattr(auditgym, name), name)
        for name in PRESETS:
            ep = generate(31, {"preset": name})
            self.assertEqual(check_episode(ep), [], name)
            if name.startswith("clean"):
                self.assertEqual(ep.truth["issues"], [])
        types = {i["type"] for i in generate(8, {"preset": "interacting"}).truth["issues"]}
        self.assertEqual(types, {"fx_rate", "payment_allocation", "duplicate_invoice"})

    def test_function_policy_hang_and_crash(self):
        self.assertEqual(run_policy(silent, seed=2, config={"preset": "clean"})["grader"]["reward"], 1.0)

        def hang(obs):
            while True:
                pass
        r = run_policy(hang, seed=2, config={"preset": "clean"}, time_limit_s=1.0, isolated=False)
        self.assertEqual(r["grader"]["end_reason"], "time_limit")

        def crash(obs):
            raise RuntimeError("ошибка в политике")
        r = run_policy(crash, seed=2, config={"preset": "mixed"}, isolated=False)
        self.assertEqual((r["grader"]["end_reason"], r["grader"]["reward"]), ("step_limit", -1.0))

    def test_baseline_adapters(self):
        rw = {k: run_policy(f(0), seed=6, config={"preset": "mixed"}, isolated=False)["grader"]["reward"]
              for k, f in BASELINE_ADAPTERS.items()}
        self.assertEqual(rw["oracle_grader_only"], 1.0)
        self.assertEqual(rw["flag_all_attack"], -1.0)


class FileEpisodes(unittest.TestCase):
    def test_env_from_files_and_external_transform(self):
        with tempfile.TemporaryDirectory() as d:
            ep = generate(9, {"preset": "mixed"})
            write_episode(ep, Path(d) / "a")
            new = transform_episode(Path(d) / "a", ep.episode_id, Path(d) / "b", salt="zz", order_seed=3)
            pa = load_episode_files(Path(d) / "a", ep.episode_id)[0]
            pb = load_episode_files(Path(d) / "b", new)
            self.assertFalse({r["row_id"] for r in pa["ledger"]} & {r["row_id"] for r in pb[0]["ledger"]})
            self.assertEqual(check_records(*pb), [])
            env = AuditEnv(mode="eval")
            ra = [run_episode(env, make_policy(p), 0, {"episode_files": {"root": str(Path(d) / "a"), "episode_id": ep.episode_id}})["reward"]
                  for p in ("oracle", "rule_auditor")]
            rb = [run_episode(env, make_policy(p), 0, {"episode_files": {"root": str(Path(d) / "b"), "episode_id": new}})["reward"]
                  for p in ("oracle", "rule_auditor")]
            self.assertEqual(ra, rb)
            self.assertEqual(ra[0], 1.0)
            with self.assertRaises(ConfigError):
                env.reset(0, {"episode_files": {"root": d, "episode_id": "EP-NOPE"}})
            with self.assertRaises(ConfigError):
                env.reset(0, {"episode_files": {"root": d, "episode_id": ep.episode_id}, "errors": {}})


if __name__ == "__main__":
    unittest.main()
