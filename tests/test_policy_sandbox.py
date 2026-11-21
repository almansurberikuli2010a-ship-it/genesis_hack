"""Изоляция политики в run_policy и канал оценщика: атаки «пробелов 1–4» прошлого аудита
(стек оркестратора, клиент с токеном в памяти, подмена отчёта, запись в код оценщика), чтение и
запись файлов эталона на диске, Oracle, токен вне окружения, команды только с токеном, падающие
действия. Политики-атаки описаны на уровне модуля: в процесс политики они передаются по имени."""
import gc
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auditgym
from auditgym import server
from auditgym.adapters import BASELINE_ADAPTERS, POLICY_PROCESS_ENV, policy_sandbox, run_policy
from auditgym.env import AuditEnv
from auditgym.export import write_episode
from auditgym.generator import generate
from auditgym.policies import Oracle, Policy, is_grader_oracle, make_policy
from auditgym.runner import run_episode
from auditgym.server import RemoteError, SubprocessEnv, handle
from tests.helpers import ERRS

ROOT = Path(auditgym.__file__).resolve().parent.parent
SANDBOX = policy_sandbox() == "sandbox-exec"
NO_SANDBOX = "полноценная песочница процесса политики есть только на macOS (sandbox-exec)"


def empty(eid):
    return {"type": "submit", "findings": {"episode_id": eid, "findings": []}}


def from_truth(eid, truth):
    return {"type": "submit", "findings": {"episode_id": eid, "findings": [
        {"type": i["type"], "affected_ids": i["affected_ids"], "claimed_amount_minor": i["expected_amount_minor"],
         "currency": i["currency"], "evidence_ids": i["evidence_sets"][0], "explanation": "x"} for i in truth["issues"]]}}


class Cheat:
    name = "cheat"

    def begin(self, obs):
        self.eid = obs["episode_id"]


class StackSeed(Cheat):
    """Пробел 1: seed и config из стека вызовов оркестратора."""

    def act(self, obs):
        f = sys._getframe(1)
        while f is not None:
            if {"seed", "config", "policy"} <= set(f.f_locals):
                return from_truth(self.eid, generate(f.f_locals["seed"], f.f_locals["config"]).truth)
            f = f.f_back
        return empty(self.eid)


class GcOracle(Cheat):
    """Пробел 2: клиент среды с токеном оценщика в памяти своего процесса."""

    def act(self, obs):
        for o in gc.get_objects():
            if type(o).__name__ in ("SubprocessEnv", "AuditEnv") and hasattr(o, "oracle_findings"):
                try:
                    found = o.oracle_findings()
                except Exception:
                    continue
                if found.get("episode_id") == self.eid:
                    return {"type": "submit", "findings": found}
        return empty(self.eid)


class PatchReport(Cheat):
    """Пробел 3: подмена grader_report в своём процессе."""

    def act(self, obs):
        from auditgym import env as env_mod

        def fake(self):
            return {"result": {"reward": 1.0}}
        server.SubprocessEnv.grader_report = fake
        env_mod.AuditEnv.grader_report = fake
        return empty(self.eid)


class DeepAction(Cheat):
    """Действие — JSON-строка с вложенностью 100 000."""

    def act(self, obs):
        return "[" * 100_000 + "]" * 100_000


class Probe(Cheat):
    """Пробует прочитать и изменить файлы эталона и кода; первым действием сообщает, что удалось, затем
    отправляет эталон, если прочитал его."""

    def __init__(self, root, sandboxed):
        self.root, self.sandboxed = root, sandboxed

    def act(self, obs):
        if obs["step"]:
            return from_truth(self.eid, self.truth) if self.truth else empty(self.eid)
        g = Path(self.root) / "grader" / self.eid
        res, self.truth = {}, None

        def attempt(name, fn):
            try:
                fn()
                res[name] = True
            except Exception:
                res[name] = False
        attempt("read_truth", lambda: setattr(self, "truth", json.loads((g / "truth.json").read_text(encoding="utf-8"))))
        attempt("read_provenance", lambda: (g / "provenance.json").read_bytes())
        attempt("read_pre_injection", lambda: (g / "pre_injection" / "ledger.csv").read_bytes())
        attempt("list_grader_dir", lambda: os.listdir(g))
        attempt("read_open_episode_truth",
                lambda: next((ROOT / "episodes").glob("*/grader/*/truth.json")).read_bytes())
        attempt("symlink_read", lambda: (os.symlink(g / "truth.json", "link.json"), Path("link.json").read_bytes()))
        attempt("hardlink", lambda: os.link(g / "truth.json", "hard.json"))
        attempt("write_truth", lambda: open(g / "truth.json", "a").close())
        attempt("create_in_grader", lambda: (g / "new.json").write_text("{}"))
        attempt("create_in_public", lambda: (Path(self.root) / "public" / "x.txt").write_text("x"))
        attempt("open_code_for_write", lambda: open(ROOT / "auditgym" / "matcher.py", "a").close())  # ничего не пишет
        if self.sandboxed:  # без песочницы не мусорим в коде среды
            attempt("create_in_code", lambda: (ROOT / "auditgym" / "__pycache__" / "probe.pyc").write_bytes(b""))
        attempt("signal_parent", lambda: os.kill(os.getppid(), signal.SIGWINCH))  # по умолчанию игнорируется
        attempt("write_cwd", lambda: Path("probe.txt").write_text("x"))
        attempt("write_tmpdir", lambda: tempfile.TemporaryFile().close())
        res["cwd_is_tmpdir"] = os.path.realpath(os.getcwd()) == os.path.realpath(tempfile.gettempdir())
        res["cwd_private"] = Path(os.getcwd()).name.startswith("auditgym-policy-")
        res["auditgym_env"] = sorted(k for k in os.environ if k.startswith("AUDITGYM"))
        return {"type": "probe", "results": res}


class SubOracle(Oracle):
    """Подкласс Oracle с подменённым act."""

    def act(self, obs):
        try:
            open(ROOT / "auditgym" / "matcher.py", "a").close()  # ничего не пишет
            wrote = True
        except OSError:
            wrote = False
        return {"type": "probe", "results": {"open_code_for_write": wrote}} if obs["step"] == 0 else empty(self.episode_id)


class Spy(Policy):
    name = "spy"
    calls = []

    def act(self, obs):
        Spy.calls.append(obs)
        return empty(self.episode_id)


def reward(r):
    return r["grader"]["reward"]


class GapsOfPreviousAudit(unittest.TestCase):
    """Пробелы 1–4: те же атаки в доверенном запуске (isolated=False) дают ответ, в официальном — нет."""

    def test_gap1_stack_seed(self):
        self.assertEqual(reward(run_policy(StackSeed(), seed=5, config=ERRS, isolated=False)), 1.0)  # атака настоящая
        r = run_policy(StackSeed(), seed=5, config=ERRS)
        self.assertEqual((reward(r), r["grader"]["tp"]), (-1.0, 0))

    def test_gap2_gc_oracle(self):
        self.assertEqual(reward(run_policy(GcOracle(), seed=5, config=ERRS, isolated=False)), 1.0)
        self.assertEqual(reward(run_policy(GcOracle(), seed=5, config=ERRS)), -1.0)

    def test_gap3_patch_report(self):
        real = (server.SubprocessEnv.grader_report, AuditEnv.grader_report)
        r = run_policy(PatchReport(), seed=5, config=ERRS)
        self.assertEqual((reward(r), r["grader"]["tp"]), (-1.0, 0))
        self.assertEqual((server.SubprocessEnv.grader_report, AuditEnv.grader_report), real)

    @unittest.skipUnless(SANDBOX, NO_SANDBOX)
    def test_gap4_tamper_grader_code_in_copy(self):
        """Политика, загруженная по пути к файлу, пишет в matcher.py копии среды; копия не меняется,
        следующий эпизод с пустым ответом получает −1."""
        sol = ('from pathlib import Path\n'
               'import auditgym\n'
               'class Tamper:\n'
               '    name = "tamper"\n'
               '    def begin(self, obs):\n'
               '        self.eid = obs["episode_id"]\n'
               '    def act(self, obs):\n'
               '        pkg = Path(auditgym.__file__).resolve().parent\n'
               '        for target, data in ((pkg / "matcher.py", "\\ndef terminal_reward(f1, s):\\n    return 1.0\\n"),\n'
               '                             (pkg / "__pycache__" / "matcher.cpython-3.pyc", "x"), (pkg / "sitecustomize.py", "x"),\n'
               '                             (pkg.parent / "usercustomize.py", "x")):\n'
               '            try:\n'
               '                target.parent.mkdir(exist_ok=True)\n'
               '                with open(target, "a") as f:\n'
               '                    f.write(data)\n'
               '            except OSError:\n'
               '                pass\n'
               '        return {"type": "submit", "findings": {"episode_id": self.eid, "findings": []}}\n')
        script = ('import importlib.util, json, sys\n'
                  'sys.path.insert(0, sys.argv[1])\n'
                  'from auditgym.adapters import run_policy\n'
                  'spec = importlib.util.spec_from_file_location("solution", sys.argv[2])\n'
                  'mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n'
                  'cfg = json.loads(sys.argv[3])\n'
                  'print(json.dumps([run_policy(mod.Tamper(), seed=5, config=cfg)["grader"]["reward"],\n'
                  '                  run_policy(lambda obs: {"type": "submit", "findings": {"episode_id": obs["episode_id"],\n'
                  '                             "findings": []}}, seed=5, config=cfg)["grader"]["reward"]]))\n')
        with tempfile.TemporaryDirectory() as d:
            copy = Path(d) / "env"
            shutil.copytree(ROOT / "auditgym", copy / "auditgym", ignore=shutil.ignore_patterns("__pycache__"))
            (Path(d) / "solution.py").write_text(sol, encoding="utf-8")

            def snapshot():
                return {str(p.relative_to(copy)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(copy.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
            before = snapshot()
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            p = subprocess.run([sys.executable, "-c", script, str(copy), str(Path(d) / "solution.py"), json.dumps(ERRS)],
                               capture_output=True, text=True, timeout=300, cwd=d, env=env)
            self.assertEqual(p.returncode, 0, p.stderr[-2000:])
            self.assertEqual(json.loads(p.stdout.strip().splitlines()[-1]), [-1.0, -1.0])
            self.assertEqual(snapshot(), before)
            self.assertFalse((copy / "usercustomize.py").exists())
            self.assertFalse(any((copy / "auditgym" / "__pycache__").glob("matcher.cpython-3.pyc")))


class GraderFilesOnDisk(unittest.TestCase):
    """Эпизод из файлов (episode_files, generate --out): процесс политики не читает и не меняет эталон."""

    def test_policy_cannot_read_or_write_grader_files(self):
        with tempfile.TemporaryDirectory() as d:
            ep = generate(5, ERRS)
            write_episode(ep, Path(d))
            truth_path = Path(d) / "grader" / ep.episode_id / "truth.json"
            truth_before = truth_path.read_bytes()
            r = run_policy(Probe(d, SANDBOX), seed=0, config={"episode_files": {"root": d, "episode_id": ep.episode_id}})
            res = r["trajectory"][1]["action"]["results"]
            self.assertEqual(r["trajectory"][1]["observation"]["error"]["code"], "unknown_action")
            self.assertTrue(res["write_cwd"] and res["write_tmpdir"] and res["cwd_is_tmpdir"] and res["cwd_private"], res)
            self.assertEqual(res["auditgym_env"], [POLICY_PROCESS_ENV], res)
            self.assertEqual(truth_path.read_bytes(), truth_before)
            if not SANDBOX:
                self.skipTest(NO_SANDBOX)
            self.assertFalse((Path(d) / "grader" / ep.episode_id / "new.json").exists())
            denied = {k: v for k, v in res.items() if k not in ("write_cwd", "write_tmpdir", "cwd_is_tmpdir", "cwd_private",
                                                                "auditgym_env")}
            self.assertEqual({k for k, v in denied.items() if v}, set(), res)
            self.assertEqual((reward(r), r["grader"]["tp"]), (-1.0, 0))
            self.assertFalse((Path(d) / "public" / "x.txt").exists())


class OraclePrivilege(unittest.TestCase):
    def test_only_oracle_created_by_env_gets_answer(self):
        self.assertTrue(is_grader_oracle(make_policy("oracle")))
        self.assertTrue(is_grader_oracle(BASELINE_ADAPTERS["oracle_grader_only"](0)))
        fake = Spy()
        fake.__class__ = Oracle
        for p in (Oracle(), SubOracle(), fake, "oracle"):
            self.assertFalse(is_grader_oracle(p), p)
        env = AuditEnv(mode="eval")
        self.assertEqual(run_episode(env, make_policy("oracle"), 5, ERRS)["reward"], 1.0)
        self.assertEqual(run_episode(env, Oracle(), 5, ERRS)["reward"], -1.0)
        self.assertEqual(run_episode(env, SubOracle(), 5, ERRS)["reward"], -1.0)

    def test_run_policy_oracle_requests_and_sent_objects(self):
        for req in ("oracle", BASELINE_ADAPTERS["oracle_grader_only"](0), make_policy("oracle")):
            self.assertEqual(reward(run_policy(req, seed=5, config=ERRS)), 1.0)
        self.assertEqual(reward(run_policy(BASELINE_ADAPTERS["oracle_grader_only"](0), seed=5, config=ERRS, isolated=False)), 1.0)
        # присланный объект Oracle или подкласс — в процесс политики, без ответа
        self.assertEqual(reward(run_policy(Oracle(), seed=5, config=ERRS)), -1.0)
        self.assertEqual(reward(run_policy(Oracle(), seed=5, config=ERRS, isolated=False)), -1.0)
        r = run_policy(SubOracle(), seed=5, config=ERRS)
        self.assertEqual(reward(r), -1.0)
        if SANDBOX:
            self.assertFalse(r["trajectory"][1]["action"]["results"]["open_code_for_write"])
        # у объекта-запроса oracle ничего не вызывается: в процессе оркестратора работает свежая oracle среды
        req = make_policy("oracle")
        req.act = Spy().act
        Spy.calls.clear()
        self.assertEqual(reward(run_policy(req, seed=5, config=ERRS)), 1.0)
        self.assertEqual(Spy.calls, [])

    def test_baseline_adapters_isolated(self):
        rw = {k: reward(run_policy(f(0), seed=6, config={"preset": "mixed"})) for k, f in BASELINE_ADAPTERS.items()}
        self.assertEqual(rw["oracle_grader_only"], 1.0)
        self.assertEqual(rw["flag_all_attack"], -1.0)
        self.assertLess(rw["random_explorer"], 1.0)


class GraderChannel(unittest.TestCase):
    def test_token_not_in_environment_or_arguments(self):
        seen = {}
        real = subprocess.Popen

        def spy(*a, **kw):
            seen.update(args=a[0], env=kw.get("env"))
            return real(*a, **kw)
        with mock.patch.object(server.subprocess, "Popen", spy):
            env = SubprocessEnv(mode="eval")
        with env:
            token = env._token
            self.assertNotIn(token, json.dumps(seen["args"]))
            self.assertNotIn(token, json.dumps(seen["env"]))
            self.assertNotIn(token, json.dumps(dict(os.environ)))
            self.assertFalse(hasattr(server, "TOKEN_ENV"))
            env.reset(5, ERRS)
            self.assertIsNone(env.grader_report()["result"])  # токен дошёл до процесса через stdin

    def test_agent_without_token_can_only_step(self):
        with SubprocessEnv(mode="eval") as env:
            env.reset(5, ERRS)
            for req in ({"cmd": "reset", "seed": 1, "config": {}}, {"cmd": "reset", "seed": 1, "token": "guess"},
                        {"cmd": "grader_report"}, {"cmd": "oracle", "token": None}, {"cmd": "close"},
                        {"cmd": "close", "token": ["x"]}):
                with self.assertRaises(RemoteError) as ctx:
                    env._call(req)
                self.assertEqual(str(ctx.exception), "forbidden", req)
            o = env._call({"cmd": "step", "action": {"type": "list_records", "table": "fx", "limit": 2}})["obs"]
            self.assertEqual((o["step"], len(o["result"]["rows"])), (1, 2))  # процесс жив, эпизод тот же
            rep = env.grader_report()
            self.assertEqual((rep["episode_id"], rep["finished"]), (generate(5, ERRS).episode_id, False))

    def test_open_mode_for_external_agents(self):
        out = io.StringIO()
        lines = [json.dumps({"cmd": "reset", "seed": 5, "config": ERRS}),
                 json.dumps({"cmd": "step", "action": {"type": "calculator", "expression": "2+2"}}),
                 json.dumps({"cmd": "grader_report"}), json.dumps({"cmd": "oracle", "token": ""}), "[1]", "{oops",
                 json.dumps({"cmd": "close"}), json.dumps({"cmd": "step", "action": None})]
        server.serve("eval", 60.0, stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
        resp = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertTrue(resp[0]["ok"])
        self.assertEqual(resp[1]["obs"]["result"], {"value": "4"})
        self.assertEqual([r.get("error") for r in resp[2:]], ["forbidden", "forbidden", "invalid request", "invalid request"])

    def test_token_mode_reads_token_from_first_line(self):
        out = io.StringIO()
        lines = ["secret-token", json.dumps({"cmd": "reset", "seed": 5, "config": ERRS}),
                 json.dumps({"cmd": "reset", "seed": 5, "config": ERRS, "token": "secret-token"}),
                 json.dumps({"cmd": "grader_report", "token": "secret-token"}), json.dumps({"cmd": "close"}),
                 json.dumps({"cmd": "close", "token": "secret-token"}), json.dumps({"cmd": "step", "action": None})]
        server.serve("eval", 60.0, token_stdin=True, stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
        resp = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r["ok"] for r in resp], [False, True, True, False])  # после close с токеном — конец
        self.assertEqual((resp[0]["error"], resp[3]["error"]), ("forbidden", "forbidden"))


class BrokenActionsThroughProcesses(unittest.TestCase):
    def test_deep_or_unencodable_actions_cost_a_step(self):
        deep = None
        for _ in range(100_000):
            deep = [deep]
        with SubprocessEnv(mode="eval") as env:
            env.reset(5, ERRS)
            for i, a in enumerate(["[" * 100_000 + "]" * 100_000, deep, {1, 2}, object(), "x" * 2_000_000], 1):
                o, r, te, tr, info = env.step(a)
                self.assertEqual((o["step"], o["error"]["code"], te, tr), (i, "unknown_action", False, False))
            # строка протокола, которую процесс среды не может разобрать, но это step: шаг израсходован
            for i, line in enumerate(['{"cmd": "step", "action": ' + "[" * 100_000 + "]" * 100_000 + "}",
                                      '{"action": ' + "{\"a\": " * 50_000 + "1" + "}" * 50_000 + ', "cmd": "step"}',
                                      '{"cmd": "step", "action": {"type": "x" ' + " " * (server.MAX_REQUEST_CHARS + 10) + "}}"],
                                     6):
                o = env._send(line)["obs"]
                self.assertEqual((o["step"], o["error"]["code"]), (i, "unknown_action"))
            with self.assertRaises(RemoteError) as ctx:
                env._send("[" * 100_000)
            self.assertEqual(str(ctx.exception), "invalid request")
            self.assertEqual(env.step({"type": "calculator", "expression": "1"})[0]["step"], 9)

    def test_exception_inside_step_is_a_bounded_error(self):
        class Boom(AuditEnv):
            def step(self, action):
                if action == "boom":
                    raise ValueError("внутренняя ошибка")
                return super().step(action)
        env = Boom()
        env.reset(5, ERRS)
        resp = handle(env, json.dumps({"cmd": "step", "action": "boom"}), None)
        self.assertTrue(resp["ok"])
        self.assertEqual((resp["obs"]["step"], resp["obs"]["error"]["code"]), (1, "unknown_action"))
        self.assertEqual(handle(AuditEnv(), json.dumps({"cmd": "step", "action": None}), None),
                         {"ok": False, "error": "internal error"})  # до reset шага нет

    def test_run_policy_with_deep_action(self):
        r = run_policy(DeepAction(), seed=5, config=ERRS)
        self.assertEqual((r["grader"]["end_reason"], r["summary"]["steps"], reward(r)), ("step_limit", 100, -1.0))
        self.assertTrue(all(t["observation"]["error"]["code"] == "unknown_action" for t in r["trajectory"][1:]))


class PolicyProcess(unittest.TestCase):
    def test_no_nested_run_policy_and_cleanup(self):
        before = set(Path(tempfile.gettempdir()).glob("auditgym-policy-*"))
        run_policy(StackSeed(), seed=2, config={"preset": "clean"})
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("auditgym-policy-*")), before)
        with mock.patch.dict(os.environ, {POLICY_PROCESS_ENV: "1"}):
            with self.assertRaises(RuntimeError):
                run_policy(StackSeed(), seed=2, config={"preset": "clean"})


if __name__ == "__main__":
    unittest.main()
