"""Tests for token_audit.py on synthetic logs. Standard library only, no network."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "examples"))

import make_synthetic_logs as synth  # noqa: E402
import token_audit as ta  # noqa: E402

DEMO = os.path.join(REPO, "examples", "demo-project")
SAMPLE = os.path.join(REPO, "examples", "sample-report.md")


def run_cli(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        ta.main(list(args))
    return buf.getvalue()


class Synthetic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.projects = synth.build(cls.tmp)
        cls.json_path = os.path.join(cls.tmp, "agg.json")
        cls.out_path = os.path.join(cls.tmp, "report.md")
        cls.report = run_cli("--projects", cls.projects, "--repo", DEMO, "--today", "2026-01-07",
                             "--json", cls.json_path, "--out", cls.out_path)
        with open(cls.json_path, encoding="utf-8") as f:
            cls.json_text = f.read()
        cls.sessions = run_cli("--projects", cls.projects, "--list-sessions")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestPrivacy(Synthetic):
    def test_no_message_content_in_any_output(self):
        for name, text in (("report", self.report), ("json", self.json_text), ("sessions", self.sessions)):
            self.assertNotIn(synth.MARKER, text, name)

    def test_marker_really_is_in_the_logs(self):
        found = False
        for root, _d, files in os.walk(self.projects):
            for fn in files:
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    found = found or synth.MARKER in f.read()
        self.assertTrue(found, "the privacy test would prove nothing")


class TestNumbers(Synthetic):
    def runs(self):
        return ta.collect(self.projects, ta.dt.datetime(2000, 1, 1))

    def test_split_reply_is_counted_once(self):
        mains = [r for r in self.runs() if r["agent"] == ta.MAIN and r["title"] == "Checkout bug"]
        self.assertEqual(len(mains), 1)
        path = mains[0]["path"]
        with open(path, encoding="utf-8") as f:
            ids = {(json.loads(ln)["message"]["id"], json.loads(ln)["requestId"])
                   for ln in f if '"type": "assistant"' in ln}
        self.assertEqual(mains[0]["requests"], len(ids))

    def test_weighted_tokens(self):
        u = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
             "output_tokens": 2, "cache_creation": {"ephemeral_1h_input_tokens": 40}}
        self.assertAlmostEqual(ta.wt(u), 10 + 1.25 * 60 + 2 * 40 + 0.1 * 1000 + 5 * 2)

    def test_carry(self):
        self.assertAlmostEqual(ta.carry_wt(3500, 0, 11), 1000 * (1.25 + 0.1 * 10))
        self.assertAlmostEqual(ta.carry_wt(3500, 10, 11), 1000 * 1.25)

    def test_subagents_are_named_from_meta(self):
        agents = {r["agent"] for r in self.runs()}
        self.assertEqual(agents, {ta.MAIN, "code-reviewer", "browser-checker", "docs-writer"})

    def test_screenshot_tokens(self):
        bc = [r for r in self.runs() if r["agent"] == "browser-checker"]
        imgs = sum(ta.run_images(r)[0] for r in bc)
        tok = sum(ta.run_images(r)[1] for r in bc)
        self.assertEqual(imgs, 12 * 2 + 8)
        self.assertEqual(tok, imgs * (1280 * 800 // 750))


class TestFindings(Synthetic):
    def test_unused_tool_grants_are_exactly_the_never_called_ones(self):
        line = next(ln for ln in self.report.splitlines() if "unused tool grant** - `browser-checker`" in ln)
        listed = re.search(r"`tools:` line: ([^)]*)\)", line).group(1).split(", ")
        self.assertEqual(sorted(listed), sorted(["Read", "Grep", "WebFetch", "mcp__browser__form_input",
                                                 "mcp__browser__upload_file", "mcp__browser__console_messages"]))

    def test_expected_kinds_are_found(self):
        for kind in ("short subagent runs** - `code-reviewer`", "screenshots** - `browser-checker`",
                     "repeated read** - `docs/api-spec.md`", "preloaded skill** - `docs-writer`"):
            self.assertIn(kind, self.report)

    def test_read_paths_are_relative_to_the_project(self):
        self.assertNotIn(synth.CWD, self.report)

    def test_paths_outside_the_project_hide_the_home_folder(self):
        self.assertEqual(ta.short_path(r"D:\Data\someone\notes\a.md", "/srv/x", "D:/Data/someone/"), "~/notes/a.md")
        self.assertEqual(ta.short_path("/srv/x/src/a.py", "/srv/x", "/root/"), "src/a.py")

    def test_typed_command_tag_is_not_a_skill(self):
        run = ta.new_run(ta.MAIN, "s", "p")
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "t.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "user", "message": {"content": "look <command-name>x:y</command-name>"}}) + chr(10))
                f.write(json.dumps({"type": "user", "message": {"content": "<command-name>/a:b</command-name>"}}) + chr(10))
            self.assertEqual([s for _, s in ta.scan_transcript(p, run)["skills"]], ["a:b"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_heavy_model_on_short_runs_is_flagged(self):
        self.assertIn("- `code-reviewer` (opus/high)", self.report)

    def test_sessions_are_listed_by_name(self):
        self.assertIn("| Checkout bug |", self.sessions)
        self.assertIn("| Pricing page copy |", self.sessions)

    def test_out_file_matches_stdout(self):
        with open(self.out_path, encoding="utf-8") as f:
            self.assertIn(f.read().strip(), self.report)


class TestSampleReport(Synthetic):
    def test_committed_sample_is_a_real_run(self):
        """examples/sample-report.md must be exactly what the script prints on the synthetic logs."""
        with open(SAMPLE, encoding="utf-8") as f:
            sample = f.read()
        with open(self.out_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), sample)


class TestRobustness(unittest.TestCase):
    def test_missing_folder_and_garbage_lines(self):
        tmp = tempfile.mkdtemp()
        try:
            self.assertIn("No runs", run_cli("--projects", os.path.join(tmp, "nope")))
            p = os.path.join(tmp, "projects", "-x")
            os.makedirs(p)
            with open(os.path.join(p, "s.jsonl"), "w", encoding="utf-8") as f:
                f.write("not json\n[1, 2]\n{\"type\": \"assistant\", \"message\": {\"id\": \"m\", \"usage\": {\"output_tokens\": 5}}}\n")
            out = run_cli("--projects", os.path.join(tmp, "projects"), "--repo", tmp)
            self.assertIn("api-request-shape", out)  # old logs: says what it cannot measure
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_projects_follows_claude_config_dir(self):
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        try:
            os.environ["CLAUDE_CONFIG_DIR"] = os.path.join("x", "cfg")
            self.assertEqual(ta.default_projects(), os.path.join("x", "cfg", "projects"))
            del os.environ["CLAUDE_CONFIG_DIR"]
            self.assertTrue(ta.default_projects().endswith(os.path.join(".claude", "projects")))
        finally:
            if old is not None:
                os.environ["CLAUDE_CONFIG_DIR"] = old

    def test_runs_in_isolated_mode_with_help(self):
        r = subprocess.run([sys.executable, "-I", os.path.join(REPO, "token_audit.py"), "--help"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--projects", r.stdout)


class TestRepoHygiene(unittest.TestCase):
    def files(self):
        for dirpath, dirnames, files in os.walk(REPO):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for f in files:
                if f.endswith((".md", ".py", ".yml", ".txt", ".json")):
                    yield os.path.join(dirpath, f)

    def read(self, p):
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_no_local_paths_or_email_addresses(self):
        pat = re.compile(r"[A-Za-z]:[/\x5c]Users[/\x5c]|/home/\w+/|/Users/\w+/|[\w.+-]+@[\w-]+\.[a-z]{2,}")
        bad = [f"{os.path.relpath(p, REPO)}: {m.group(0)}" for p in self.files()
               for m in pat.finditer(self.read(p)) if "noreply" not in m.group(0)]
        self.assertEqual(bad, [])

    def test_no_real_session_logs_committed(self):
        found = [os.path.join(d, f) for d, dirs, files in os.walk(REPO)
                 if ".git" not in d and "demo-logs" not in d  # README demo output, git-ignored
                 for f in files if f.endswith((".jsonl", ".meta.json"))]
        self.assertEqual(found, [])

    def test_agent_frontmatter(self):
        txt = self.read(os.path.join(REPO, ".claude", "agents", "token-auditor.md"))
        fm = dict(re.findall(r"^(\w+):\s*(.+)$", re.match(r"---\n(.*?)\n---\n", txt, re.S).group(1), re.M))
        self.assertEqual(fm["name"], "token-auditor")
        self.assertIn("Persona:", fm["description"])
        self.assertNotIn("Agent", fm["tools"].split(", "))
        self.assertIn("never message content", txt)


if __name__ == "__main__":
    unittest.main()
