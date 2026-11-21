"""Контракт reset/step, лимиты, завершение, неверный ввод, изоляция, утечки, воспроизводимость."""
import json
import unittest

from auditgym.config import ConfigError
from auditgym.env import MAX_ACTION_CHARS, MAX_STEPS, AuditEnv
from tests.helpers import ERRS, episode, oracle_findings, submit

FORBIDDEN = {"issue_id", "issues", "expected_amount_minor", "evidence_sets", "mutation_id", "mutations", "story_hash",
             "truth", "seed", "tolerance_minor", "ledger_diff", "canonical_ids"}


def keys(o, acc=None):
    acc = set() if acc is None else acc
    if isinstance(o, dict):
        for k, v in o.items():
            acc.add(k)
            keys(v, acc)
    elif isinstance(o, list):
        for v in o:
            keys(v, acc)
    return acc


class Contract(unittest.TestCase):
    def test_reset_returns_obs_info(self):
        obs, info = AuditEnv().reset(5, ERRS)
        for k in ("task", "manifest", "policy", "steps_remaining", "episode_id", "actions"):
            self.assertIn(k, obs)
        self.assertEqual(obs["steps_remaining"], MAX_STEPS)
        self.assertEqual(set(info), {"episode_id", "mode", "env_version"})
        json.dumps(obs, ensure_ascii=False), json.dumps(info)
        for k in ("schema_version", "episode_id", "base_currency", "period_start", "period_end", "timezone", "counts",
                  "rounding", "currency_exponents"):
            self.assertIn(k, obs["manifest"])
        self.assertIn("аудит", obs["task"])  # текст задачи — на русском

    def test_step_returns_five_tuple(self):
        env = AuditEnv()
        env.reset(5, ERRS)
        obs, r, te, tr, info = env.step({"type": "list_records", "table": "ledger", "cursor": None, "limit": 10})
        self.assertEqual((r, te, tr), (0.0, False, False))
        self.assertEqual(len(obs["result"]["rows"]), 10)
        self.assertEqual(obs["steps_remaining"], MAX_STEPS - 1)

    def test_page_limit_and_stable_cursors(self):
        env = AuditEnv()
        obs, _ = env.reset(5, ERRS)
        seen, cursor = [], None
        while True:
            o = env.step({"type": "list_records", "table": "ledger", "cursor": cursor, "limit": 1000})[0]
            self.assertLessEqual(len(o["result"]["rows"]), 50)
            seen += [r["row_id"] for r in o["result"]["rows"]]
            cursor = o["result"]["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(seen), obs["manifest"]["counts"]["ledger"])
        self.assertEqual(len(set(seen)), len(seen))
        a = env.step({"type": "list_records", "table": "ledger", "cursor": "ledger:50", "limit": 5})[0]
        b = env.step({"type": "list_records", "table": "ledger", "cursor": "ledger:50", "limit": 5})[0]
        self.assertEqual(a["result"], b["result"])

    def test_all_tools(self):
        ep = episode(5, ERRS)
        env = AuditEnv()
        env.reset(5, ERRS)
        inv, pay, row, fx = ep.public["invoices"][0], ep.public["payments"][0], ep.public["ledger"][0], ep.public["fx"][0]
        self.assertEqual(env.step({"type": "get_record", "table": "invoices", "id": inv["invoice_id"]})[0]["result"]["record"], inv)
        self.assertEqual(env.step({"type": "get_record", "table": "invoices", "id": inv["doc_id"]})[0]["result"]["record"], inv)
        self.assertEqual(env.step({"type": "get_record", "table": "payments", "id": pay["payment_id"]})[0]["result"]["record"], pay)
        self.assertEqual(env.step({"type": "get_record", "table": "ledger", "id": row["row_id"]})[0]["result"]["record"], row)
        self.assertEqual(env.step({"type": "get_record", "table": "fx", "id": fx["fx_id"]})[0]["result"]["record"], fx)
        self.assertEqual(env.step({"type": "get_document", "doc_id": pay["doc_id"]})[0]["result"]["document"], pay)
        self.assertEqual(env.step({"type": "calculator", "expression": "(1+2)*3/4"})[0]["result"]["value"], "2.25")

    def test_errors_are_bounded_and_consume_steps(self):
        env = AuditEnv()
        env.reset(5, ERRS)
        bad = [{"type": "get_record", "table": "ledger", "id": "L-NOPE"}, {"type": "get_record", "table": "secret", "id": "x"},
               {"type": "get_document", "doc_id": "../truth.json"}, {"type": "rm -rf"},
               {"type": "list_records", "table": "ledger", "cursor": "fx:0"},
               {"type": "list_records", "table": "ledger", "cursor": "ledger:999999"},
               {"type": "list_records", "table": "ledger", "limit": 0}, {"type": "list_records", "table": "ledger", "limit": "50"},
               {"type": "calculator", "expression": "__import__('os').system('ls')"}, {"type": "calculator", "expression": "1/0"},
               {"type": "get_record", "table": "ledger", "id": "L-x", "sql": "select *"}, "не словарь", "{not json", None, 42]
        for i, a in enumerate(bad, 1):
            obs, r, te, tr, info = env.step(a)
            self.assertIsNotNone(obs["error"], a)
            self.assertIsNone(obs["result"])
            self.assertLess(len(obs["error"]["message"]), 120)
            self.assertEqual(obs["step"], i)
            self.assertEqual((r, te, tr, info), (0.0, False, False, {}))

    def test_json_string_actions(self):
        env = AuditEnv()
        env.reset(5, ERRS)
        obs = env.step(json.dumps({"type": "list_records", "table": "fx", "limit": 3}))[0]
        self.assertEqual(len(obs["result"]["rows"]), 3)

    def test_submit_terminates_train_reward(self):
        ep = episode(5, ERRS)
        env = AuditEnv(mode="train")
        env.reset(5, ERRS)
        env.step({"type": "list_records", "table": "fx"})
        obs, r, te, tr, info = submit(env, ep, oracle_findings(ep))
        self.assertTrue(te)
        self.assertFalse(tr)
        self.assertAlmostEqual(r, 1 - 0.002)
        self.assertEqual(info["grading"]["f1"], 1.0)
        self.assertEqual(env.grader_report()["result"]["inspection_steps"], 1)

    def test_resubmission_and_steps_after_end_change_nothing(self):
        ep = episode(5, ERRS)
        env = AuditEnv(mode="train")
        env.reset(5, ERRS)
        submit(env, ep, [])
        first = env.grader_report()["result"]
        obs, r, te, tr, info = submit(env, ep, oracle_findings(ep))
        self.assertEqual(obs["error"]["code"], "episode_finished")
        self.assertEqual((r, te), (0.0, True))
        self.assertEqual(env.grader_report()["result"], first)

    def test_eval_mode_hides_reward_and_diagnostics(self):
        ep = episode(5, ERRS)
        env = AuditEnv(mode="eval")
        env.reset(5, ERRS)
        obs, r, te, tr, info = submit(env, ep, oracle_findings(ep))
        self.assertEqual((r, te, info, obs["result"]), (0.0, True, {}, {"submitted": True}))
        self.assertEqual(env.grader_report()["result"]["reward"], 1.0)

    def test_invalid_submission_minus_one_and_grader_log(self):
        for bad in ({"type": "submit", "findings": None}, {"type": "submit"},
                    {"type": "submit", "findings": {"episode_id": "x", "findings": []}},
                    {"type": "submit", "findings": {"findings": []}, "extra": 1}):
            env = AuditEnv(mode="train")
            env.reset(5, ERRS)
            obs, r, te, tr, info = env.step(bad)
            self.assertEqual((te, r), (True, -1.0))
            self.assertEqual(env.grader_report()["log"][0]["event"], "invalid_submission")
            # политике — только ограниченная ошибка о структуре, без оценки правильности
            self.assertIsNone(obs["result"])
            self.assertEqual(obs["error"]["code"], "invalid_submission")
            self.assertLess(len(obs["error"]["message"]), 120)
            self.assertNotIn("error", info.get("grading", {}))

    def test_step_limit_truncates_with_empty_answer(self):
        env = AuditEnv(mode="train")
        env.reset(5, ERRS)
        for _ in range(MAX_STEPS - 1):
            _, r, te, tr, _ = env.step({"type": "calculator", "expression": "1+1"})
            self.assertFalse(te or tr)
        obs, r, te, tr, info = env.step({"type": "calculator", "expression": "1+1"})
        self.assertEqual((tr, te, obs["steps_remaining"], r), (True, False, 0, -1.0))
        rep = env.grader_report()["result"]
        self.assertEqual((rep["end_reason"], rep["n_findings"]), ("step_limit", 0))
        env.reset(5, {})  # submit сотым действием ещё принимается
        for _ in range(MAX_STEPS - 1):
            env.step({"type": "calculator", "expression": "1"})
        _, r, te, tr, _ = env.step({"type": "submit", "findings": {"episode_id": env._ep.episode_id, "findings": []}})
        self.assertEqual((te, tr), (True, False))
        self.assertAlmostEqual(r, 1 - 0.002 * 99)

    def test_time_limit_truncates(self):
        now = [0.0]
        env = AuditEnv(mode="train", time_limit_s=60, clock=lambda: now[0])
        env.reset(5, {})
        env.step({"type": "calculator", "expression": "1"})
        now[0] = 61.0
        obs, r, te, tr, info = env.step({"type": "submit", "findings": {"episode_id": "x", "findings": []}})
        self.assertEqual((tr, te, obs["error"]["code"]), (True, False, "time_limit"))
        rep = env.grader_report()["result"]
        self.assertEqual((rep["end_reason"], rep["n_findings"]), ("time_limit", 0))
        self.assertAlmostEqual(r, 1 - 0.002)

    def test_reset_isolates_state(self):
        env = AuditEnv(mode="train")
        o1, _ = env.reset(5, ERRS)
        for _ in range(5):
            env.step({"type": "list_records", "table": "ledger"})
        env.step({"type": "submit", "findings": {"episode_id": o1["episode_id"], "findings": []}})
        o2, _ = env.reset(5, ERRS)
        self.assertEqual(o1, o2)
        self.assertEqual(env.step({"type": "calculator", "expression": "1"})[0]["step"], 1)
        self.assertIsNone(env.grader_report()["result"])

    def test_bad_config_rejected(self):
        env = AuditEnv()
        for cfg in ({"nope": 1}, {"errors": {"fx_rate": 13}}, {"errors": {"made_up": 1}},
                    {"errors": {"fx_rate": 6, "period_cutoff": 6, "duplicate_invoice": 1}}, {"n_invoices": [5, 3]},
                    {"config_version": "9"}, {"preset": "нет"}, "{oops", '"x"', {"background_entries": [0, 999]}):
            with self.assertRaises(ConfigError):
                env.reset(1, cfg)
        with self.assertRaises(ConfigError):
            env.reset("1", {})


class Reproducibility(unittest.TestCase):
    def test_same_seed_config_actions_same_everything(self):
        acts = [{"type": "list_records", "table": t, "cursor": None, "limit": 50} for t in ("ledger", "invoices", "payments", "fx")]
        runs = []
        for _ in range(2):
            env = AuditEnv(mode="train")
            seq = list(env.reset(77, ERRS)) + [env.step(a) for a in acts]
            ep = env._ep
            seq.append(env.step({"type": "submit", "findings": {"episode_id": ep.episode_id, "findings": oracle_findings(ep)[:2]}}))
            runs.append(json.dumps(seq, sort_keys=True, ensure_ascii=False))
        self.assertEqual(runs[0], runs[1])

    def test_seed_none_and_options_alias(self):
        env = AuditEnv()
        self.assertEqual(env.reset()[1]["episode_id"], env.reset(0)[1]["episode_id"])
        self.assertEqual(env.reset(4, options={"preset": "mixed"})[1]["episode_id"], env.reset(4, {"preset": "mixed"})[1]["episode_id"])
        self.assertEqual(env.reset(4, '{"preset": "mixed"}')[1]["episode_id"], env.reset(4, {"preset": "mixed"})[1]["episode_id"])
        self.assertNotEqual(env.reset(1, ERRS)[1]["episode_id"], env.reset(2, ERRS)[1]["episode_id"])


class NoLeakage(unittest.TestCase):
    def test_observations_info_errors_have_no_truth(self):
        ep = episode(5, ERRS)
        env = AuditEnv(mode="train")
        blobs = list(env.reset(5, ERRS))
        for t in ("ledger", "invoices", "payments", "fx"):
            cur = None
            while True:
                o = env.step({"type": "list_records", "table": t, "cursor": cur})[0]
                blobs.append(o)
                cur = o["result"]["next_cursor"]
                if cur is None:
                    break
        blobs.append(env.step({"type": "get_record", "table": "ledger", "id": "truth.json"}))
        blobs.append(submit(env, ep, []))
        text = json.dumps(blobs, ensure_ascii=False)
        self.assertFalse(keys(blobs) & FORBIDDEN)
        for i in ep.truth["issues"]:
            self.assertNotIn(i["issue_id"], text)
        self.assertNotIn(ep.meta["story_hash"], text)

    def test_injected_documents_not_at_the_end(self):
        ep = episode(5, ERRS)
        affected = {a for i in ep.truth["issues"] for a in i["affected_ids"]}
        ids = [i["invoice_id"] for i in ep.public["invoices"]]
        pos = [ids.index(a) for a in affected if a in ids]
        self.assertTrue(pos)
        self.assertFalse(all(p >= len(ids) - len(pos) for p in pos))


def deep(depth, leaf=None):
    """Вложенный список глубины depth без рекурсии."""
    v = leaf
    for _ in range(depth):
        v = [v]
    return v


class HostileInput(unittest.TestCase):
    """Любое неверное действие — ограниченная ошибка и израсходованный шаг, а не исключение."""

    def assert_bounded(self, env, action, step):
        obs, r, te, tr, info = env.step(action)
        self.assertIsNotNone(obs["error"])
        self.assertIsNone(obs["result"])
        self.assertLess(len(obs["error"]["message"]), 120)
        self.assertEqual((obs["step"], r, te, tr, info), (step, 0.0, False, False, {}))

    def test_deep_long_and_strange_actions(self):
        env = AuditEnv()
        env.reset(5, ERRS)
        actions = ["[" * 100_000 + "]" * 100_000, "[" * 9_999 + "]" * 9_999, '{"type": ' * 50_000 + "1" + "}" * 50_000,
                   json.dumps({"type": "calculator", "expression": "1", "pad": "x" * MAX_ACTION_CHARS}),
                   b"[" * 100_000 + b"]" * 100_000, bytearray(b"{"), deep(100_000), {"type": deep(100_000)},
                   {"type": "get_record", "table": "ledger", "id": deep(100_000)}, {"type": "calculator", "expression": deep(10)},
                   {"type": "list_records", "table": "ledger", "cursor": deep(10)}, {"type": {"a": 1}}, {"type": ["submit"]},
                   {(1, 2): 3, "type": "calculator", "expression": "1"}, float("nan"), object()]
        for i, a in enumerate(actions, 1):
            self.assert_bounded(env, a, i)
        obs = env.step({"type": "calculator", "expression": "1+1"})[0]  # среда работает дальше
        self.assertEqual((obs["result"], obs["step"]), ({"value": "2"}, len(actions) + 1))

    def test_deep_submit_is_invalid_answer(self):
        env = AuditEnv(mode="train")
        env.reset(5, ERRS)
        obs, r, te, tr, info = env.step({"type": "submit", "findings": deep(100_000)})
        self.assertEqual((r, te, tr), (-1.0, True, False))
        self.assertFalse(env.grader_report()["result"]["valid"])
        env.reset(5, ERRS)
        f = {"type": "fx_rate", "affected_ids": deep(50_000), "claimed_amount_minor": 1, "currency": "USD",
             "evidence_ids": [], "explanation": ""}
        obs, r, te, tr, info = env.step({"type": "submit", "findings": {"episode_id": env._ep.episode_id, "findings": [f]}})
        self.assertEqual((r, te), (-1.0, True))

    def test_extra_argument_names_are_not_echoed_in_full(self):
        env = AuditEnv()
        env.reset(5, ERRS)
        a = {"type": "list_records", "table": "ledger"}
        a.update({("k%d" % i) * 200_000: 1 for i in range(5)})
        obs = env.step(a)[0]
        self.assertEqual(obs["error"]["code"], "invalid_arguments")
        self.assertLess(len(obs["error"]["message"]), 120)
        self.assertEqual(obs["step"], 1)
        a = env.step({"type": "get_document", "doc_id": "x", "sql": 1})[0]["error"]["message"]
        self.assertIn("sql", a)


class Lifecycle(unittest.TestCase):
    def test_close_and_context_manager(self):
        env = AuditEnv()
        env.close()  # без эпизода — тоже без ошибки
        env.reset(5, ERRS)
        env.close()
        env.close()
        with self.assertRaises(RuntimeError):
            env.step({"type": "calculator", "expression": "1"})
        self.assertEqual(env.reset(5, ERRS)[0]["step"], 0)  # после close снова можно reset
        with AuditEnv(mode="train") as e:
            e.reset(5, ERRS)
            self.assertEqual(e.step({"type": "calculator", "expression": "2*3"})[0]["result"]["value"], "6")
        with self.assertRaises(RuntimeError):
            e.step({"type": "calculator", "expression": "1"})


if __name__ == "__main__":
    unittest.main()
