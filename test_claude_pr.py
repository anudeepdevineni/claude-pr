"""Tests for claude-pr. No API keys needed: each end-to-end test builds a real git
repo with a local "origin", and a fake server stands in for the Claude and GitHub APIs.

Run with:  python -m unittest -v
"""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import claude_pr

FAKE_PR = {
    "title": "Add greeting helper",
    "branch_name": "feat/greeting-helper",
    "commit_message": "feat: add greeting helper",
    "summary": "Adds a greet() helper and uses it from main.",
    "changes": ["`greet.py`: new greet() function", "`app.py`: prints the greeting"],
    "diagram": 'flowchart TD\n  A["app.py"] --> B["greet()"]',
    "testing": ["Run `python app.py` and check it prints Hello"],
}


class FakeAPIs(BaseHTTPRequestHandler):
    """Answers POST /v1/messages like Claude and POST /repos/.../pulls like GitHub."""
    calls = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPIs.calls.append((self.path, body))
        if self.path.startswith("/v1/messages"):
            status, reply = 200, {
                "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                "content": [{"type": "text", "text": json.dumps(FAKE_PR)}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 100},
            }
        else:
            status, reply = 201, {"html_url": "https://github.com/acme/demo/pull/1"}
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ParsingTests(unittest.TestCase):
    def test_parse_github_repo(self):
        for url in ["https://github.com/acme/demo.git", "https://github.com/acme/demo",
                    "git@github.com:acme/demo.git", "ssh://git@github.com/acme/demo.git/",
                    "https://x-access-token:abc@github.com/acme/demo.git"]:
            self.assertEqual(claude_pr.parse_github_repo(url), ("acme", "demo"), url)

    def test_body_has_sections_and_a_single_mermaid_block(self):
        pr = {**FAKE_PR, "diagram": "```mermaid\n" + FAKE_PR["diagram"] + "\n```"}
        body = claude_pr.build_pr_body(pr)
        for heading in ["## Summary", "## Architecture", "## Changes", "## How to test"]:
            self.assertIn(heading, body)
        self.assertIn('```mermaid\nflowchart TD\n  A["app.py"] --> B["greet()"]\n```', body)
        self.assertEqual(body.count("```"), 2)  # the model's own fences were stripped
        self.assertIn("- [ ] Run `python app.py`", body)


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeAPIs)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.env = mock.patch.dict(os.environ, {
            "ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": "test-key",
            "GITHUB_API_URL": url, "GITHUB_TOKEN": "test-token",
        })
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.server.shutdown()

    def setUp(self):
        FakeAPIs.calls.clear()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        root = Path(tmp.name)
        # "origin": a bare repo whose path ends in acme/demo.git, like a GitHub URL would
        self.remote = root / "acme" / "demo.git"
        self.remote.mkdir(parents=True)
        run_git(self.remote, "init", "--bare", "-b", "main")
        self.repo = root / "work"
        self.repo.mkdir()
        run_git(self.repo, "init", "-b", "main")
        for key, value in [("user.name", "Test"), ("user.email", "test@example.com"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")]:
            run_git(self.repo, "config", key, value)
        run_git(self.repo, "remote", "add", "origin", self.remote.as_posix())
        (self.repo / "app.py").write_text("print('hi')\n")
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-m", "initial")
        run_git(self.repo, "push", "-u", "origin", "main")
        # Local work: one edited file and one brand-new (untracked) file
        (self.repo / "app.py").write_text("from greet import greet\nprint(greet('world'))\n")
        (self.repo / "greet.py").write_text("def greet(name):\n    return f'Hello {name}'\n")
        os.chdir(self.repo)

    def run_tool(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            claude_pr.main(list(args))
        return out.getvalue()

    def test_creates_branch_commits_pushes_and_opens_pr(self):
        output = self.run_tool("--yes")

        self.assertIn("https://github.com/acme/demo/pull/1", output)
        (claude_path, claude_req), (github_path, github_req) = FakeAPIs.calls
        # Claude got the full change, including the untracked file, and was asked for JSON
        self.assertTrue(claude_path.startswith("/v1/messages"))
        self.assertEqual(claude_req["model"], "claude-opus-5-5")
        self.assertEqual(claude_req["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(claude_req["fallbacks"], "default")
        prompt = claude_req["messages"][0]["content"]
        self.assertIn("greet.py", prompt)
        self.assertIn("+print(greet('world'))", prompt)
        # GitHub got a PR from the new branch into main, with the Mermaid diagram in the body
        self.assertEqual(github_path, "/repos/acme/demo/pulls")
        self.assertEqual(github_req["head"], "feat/greeting-helper")
        self.assertEqual(github_req["base"], "main")
        self.assertEqual(github_req["title"], "Add greeting helper")
        self.assertIn("```mermaid\nflowchart TD", github_req["body"])
        self.assertFalse(github_req["draft"])
        # The branch and commit really landed on the remote, and nothing is left uncommitted
        self.assertEqual(run_git(self.remote, "log", "-1", "--format=%s", "feat/greeting-helper"),
                         "feat: add greeting helper")
        self.assertEqual(run_git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "feat/greeting-helper")

    def test_dry_run_changes_nothing(self):
        before = run_git(self.repo, "status", "--porcelain")

        output = self.run_tool("--dry-run")

        self.assertIn("## Architecture", output)
        self.assertEqual(len(FakeAPIs.calls), 1)  # Claude only; GitHub never called
        self.assertEqual(run_git(self.repo, "status", "--porcelain"), before)
        self.assertEqual(run_git(self.repo, "diff", "--cached", "--name-only"), "")  # index untouched
        self.assertEqual(run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_existing_feature_branch_is_reused_and_draft_flag_is_sent(self):
        run_git(self.repo, "checkout", "-b", "my-feature")

        self.run_tool("--yes", "--draft")

        github_req = FakeAPIs.calls[-1][1]
        self.assertEqual(github_req["head"], "my-feature")
        self.assertTrue(github_req["draft"])

    def test_taken_branch_name_gets_a_suffix(self):
        run_git(self.repo, "branch", "feat/greeting-helper")

        self.run_tool("--yes")

        self.assertEqual(FakeAPIs.calls[-1][1]["head"], "feat/greeting-helper-2")

    def test_no_changes_exits_without_calling_any_api(self):
        run_git(self.repo, "checkout", "--", ".")
        (self.repo / "greet.py").unlink()

        with self.assertRaises(SystemExit) as exit_info:
            self.run_tool("--yes")

        self.assertIn("No changes", str(exit_info.exception))
        self.assertEqual(FakeAPIs.calls, [])


if __name__ == "__main__":
    unittest.main()
