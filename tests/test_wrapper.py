import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(os.path.dirname(HERE), "agy_write.py")
FAKE = os.path.join(HERE, "fake_agy.py")


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.root = os.path.join(self.tmp, "proj")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.root)
        os.makedirs(self.home)
        if os.name == "nt":
            launcher = os.path.join(self.tmp, "agy.cmd")
            with open(launcher, "w") as f:
                f.write(f'@"{sys.executable}" "{FAKE}" %*\n')
        else:
            launcher = os.path.join(self.tmp, "agy")
            with open(launcher, "w") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
            os.chmod(launcher, 0o755)
        self.log = os.path.join(self.tmp, "call.json")
        self.env = dict(os.environ, HOME=self.home, USERPROFILE=self.home, AGY_BIN=launcher, FAKE_AGY_LOG=self.log)
        self.brief = os.path.join(self.tmp, "brief.md")
        with open(self.brief, "w", encoding="utf-8") as f:
            f.write("写一段话。\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_w(self, *args, env=None):
        if "--effort" not in args:
            args = ("--effort", "high") + args
        return subprocess.run([sys.executable, WRAPPER, "--root", self.root, "--prompt-file", self.brief, *args],
                              capture_output=True, text=True, env=dict(self.env, **(env or {})))

    def call(self):
        with open(self.log, encoding="utf-8") as f:
            return json.load(f)

    def meta(self, out):
        run_dir = [l for l in out.splitlines() if l.startswith("RUN_DIR: ")][0][9:]
        with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_default_run(self):
        r = self.run_w("--name", "t1")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.call()["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        self.assertIn("--sandbox", argv)
        self.assertIn("WROTE out.md PREAMBLE", r.stdout)
        m = self.meta(r.stdout)
        self.assertEqual(m["status"], "SUCCESS")
        self.assertEqual(m["conversation_id"], "conv-1")
        self.assertEqual(m["changed_files"], ["out.md"])
        self.assertEqual(len(m["files_written_by_agy"]), 1)
        self.assertEqual(m["denied_actions"], [{"action": "command"}])
        self.assertEqual(m["tool_errors"][0]["tool"], "run_command")

    def test_effort_and_model_policy(self):
        r = self.run_w("--name", "t2", "--effort", "low")
        argv = self.call()["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-low")
        r = subprocess.run([sys.executable, WRAPPER, "--root", self.root, "--prompt-file", self.brief, "--name", "t4"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 2)          # --effort is required
        self.assertIn("--effort", r.stderr)
        r = self.run_w("--name", "t3", "--model", "gpt-x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not permitted", r.stderr)

    def test_continue(self):
        r = self.run_w("--name", "c", "--continue")
        self.assertEqual(r.returncode, 2)
        self.run_w("--name", "c", env={"FAKE_AGY_CONV": "42"})
        r = self.run_w("--name", "c", "--continue")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.call()["argv"]
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-42")

    def test_failed_status_gives_nonzero(self):
        r = self.run_w("--name", "f", env={"FAKE_AGY_STATUS": "ERROR"})
        self.assertEqual(r.returncode, 1)
        r = self.run_w("--name", "g", env={"FAKE_AGY_RC": "3"})
        self.assertEqual(r.returncode, 3)

    def test_empty_prompt_and_bad_name(self):
        open(self.brief, "w").close()
        self.assertEqual(self.run_w("--name", "e").returncode, 2)
        self.assertEqual(self.run_w("--name", "bad name").returncode, 2)

    def test_concurrency_slots(self):
        cfg_dir = os.path.join(self.home, ".agy-writer")
        os.makedirs(os.path.join(cfg_dir, "locks", "slot1.lock"))
        with open(os.path.join(cfg_dir, "locks", "slot1.lock", "owner.json"), "w") as f:
            json.dump({"pid": os.getpid()}, f)          # alive: busy
        with open(os.path.join(cfg_dir, "config.json"), "w") as f:
            json.dump({"max_concurrent": 1}, f)
        self.assertEqual(self.run_w("--name", "s").returncode, 75)
        with open(os.path.join(cfg_dir, "locks", "slot1.lock", "owner.json"), "w") as f:
            json.dump({"pid": 999999}, f)               # dead: reclaimed
        self.assertEqual(self.run_w("--name", "s").returncode, 0)

    def test_stdin_prompt_and_extra_preamble(self):
        with open(os.path.join(self.root, "STYLE.md"), "w", encoding="utf-8") as f:
            f.write("STYLE RULES\n")
        with open(os.path.join(self.root, ".agy-writer.json"), "w") as f:
            json.dump({"extra_preambles": {"style": "STYLE.md"}}, f)
        r = subprocess.run([sys.executable, WRAPPER, "--root", self.root, "--name", "x", "--effort", "medium", "--with", "style"],
                           input="从标准输入来的任务", capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        content = json.loads(self.call()["stdin"])["message"]["content"]
        self.assertIn("STYLE RULES", content)
        self.assertTrue(content.rstrip().endswith("从标准输入来的任务"))


if __name__ == "__main__":
    unittest.main()
