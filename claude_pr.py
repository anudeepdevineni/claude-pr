#!/usr/bin/env python3
"""claude-pr: open a GitHub pull request straight from your local changes.

Reads the branch diff (commits + uncommitted + untracked files), asks Claude
for a title, description and Mermaid architecture diagram, then commits,
pushes and opens the PR through the GitHub REST API.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

import anthropic

MODEL = "claude-opus-5-5"
MAX_DIFF_CHARS = 400_000  # ~100K tokens; anything beyond is cut, with a warning
NOISY_FILES = [":(exclude)*.lock", ":(exclude)*-lock.json", ":(exclude)*-lock.yaml"]

SYSTEM_PROMPT = """You write GitHub pull request descriptions from a git diff. A reviewer \
should understand the change in under a minute.

Fill in:
- title: imperative mood, under 70 characters, no trailing period.
- branch_name: short kebab-case with a type prefix, e.g. "feat/payment-retries", "fix/null-user-crash".
- commit_message: a Conventional Commits subject (e.g. "feat(payments): add retry logic"), \
optionally followed by a blank line and a short body.
- summary: 2-4 plain-language sentences on what changed and why.
- changes: one item per meaningful change, naming the files or components involved. Group trivial edits.
- diagram: Mermaid source, without ``` fences, showing how the changed parts fit together: the \
components, modules or functions touched and how data or control flows between them. Use \
`flowchart TD` for structure and data flow, or `sequenceDiagram` when the change is about an \
interaction over time. Aim for 5-15 nodes. In flowcharts, mark new or modified nodes with \
`classDef changed fill:#fff3b0,stroke:#e0a800,color:#000` and `class <ids> changed`. The diagram \
must render on GitHub: put every node label in double quotes, e.g. A["parse_config()"], never use \
double quotes or markdown inside a label, and keep node ids alphanumeric.
- testing: concrete steps a reviewer can follow to verify the change.

Describe only what the diff shows; don't invent motivation or behavior you can't see."""

PR_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "branch_name": {"type": "string"},
        "commit_message": {"type": "string"},
        "summary": {"type": "string"},
        "changes": {"type": "array", "items": {"type": "string"}},
        "diagram": {"type": "string"},
        "testing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "branch_name", "commit_message", "summary", "changes", "diagram", "testing"],
    "additionalProperties": False,
}


def git(*args, env=None, check=True):
    result = subprocess.run(["git", *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def parse_github_repo(remote_url):
    """'git@github.com:owner/repo.git' or 'https://github.com/owner/repo' -> ('owner', 'repo')."""
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", remote_url)
    if not match:
        raise SystemExit(f"Can't read owner/repo from the origin URL: {remote_url}")
    return match.group(1), match.group(2)


def default_base_branch():
    ref = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)  # "origin/main"
    return ref.split("/", 1)[1] if "/" in ref else "main"


def collect_changes(base):
    """Everything the PR will contain, relative to origin/<base>: commits not yet on
    the base branch plus uncommitted and untracked files. Staging happens in a
    throwaway copy of the index, so the user's own staging area is never touched."""
    git("fetch", "--quiet", "origin", base, check=False)
    if not git("rev-parse", "--verify", "--quiet", f"origin/{base}", check=False):
        raise SystemExit(f"Base branch '{base}' isn't on origin. Push it first or pass --base.")
    merge_base = git("merge-base", "HEAD", f"origin/{base}")
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        shutil.copy(os.path.join(git("rev-parse", "--absolute-git-dir"), "index"), env["GIT_INDEX_FILE"])
        git("add", "--all", env=env)
        stat = git("diff", "--cached", "--stat", merge_base, env=env)
        diff = git("diff", "--cached", merge_base, "--", *NOISY_FILES, env=env)
    log = git("log", "--format=- %s", f"{merge_base}..HEAD")
    return stat, diff, log


def generate_pr_content(base, stat, diff, log):
    if len(diff) > MAX_DIFF_CHARS:
        print(f"warning: the diff is {len(diff):,} characters; only the first {MAX_DIFF_CHARS:,} "
              "are sent to Claude (the file list is complete).", file=sys.stderr)
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]"
    prompt = (f"Base branch: {base}\n\n"
              f"Commits already on this branch:\n{log or '(none - all changes are uncommitted)'}\n\n"
              f"Files changed:\n{stat}\n\n<diff>\n{diff}\n</diff>")
    try:
        response = anthropic.Anthropic().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": PR_SCHEMA}},
            # If a safety classifier declines, retry server-side on Anthropic's recommended model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except TypeError as e:  # what the SDK raises when no credentials are configured at all
        if "authentication" not in str(e):
            raise
        raise SystemExit("No Claude API key found. Set ANTHROPIC_API_KEY.")
    except anthropic.AuthenticationError:
        raise SystemExit("Claude API authentication failed. Check ANTHROPIC_API_KEY.")
    except anthropic.APIStatusError as e:
        raise SystemExit(f"Claude API error ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise SystemExit("Couldn't reach the Claude API. Check your connection.")
    except anthropic.AnthropicError as e:
        raise SystemExit(f"Claude API error: {e}")
    if response.stop_reason == "refusal":
        raise SystemExit("Claude declined to describe this diff.")
    if response.stop_reason == "max_tokens":
        raise SystemExit("Claude's answer was cut off. Try opening a smaller PR.")
    return json.loads(next(block.text for block in response.content if block.type == "text"))


def build_pr_body(pr):
    diagram = re.sub(r"^```(?:mermaid)?\s*|\s*```$", "", pr["diagram"].strip())
    changes = "\n".join(f"- {item}" for item in pr["changes"])
    testing = "\n".join(f"- [ ] {step}" for step in pr["testing"])
    return (f"## Summary\n{pr['summary']}\n\n"
            f"## Architecture\n```mermaid\n{diagram}\n```\n\n"
            f"## Changes\n{changes}\n\n"
            f"## How to test\n{testing}\n\n"
            "---\n_Generated by claude-pr from `git diff`._\n")


def unique_branch_name(name):
    name = re.sub(r"[^A-Za-z0-9._/-]+", "-", name).strip("-/.") or "claude-pr"
    candidate, n = name, 2
    while (git("rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}", check=False)
           or git("ls-remote", "--heads", "origin", candidate, check=False)):
        candidate, n = f"{name}-{n}", n + 1
    return candidate


def github_token():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token and shutil.which("gh"):
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    if not token:
        raise SystemExit("No GitHub token found. Set GITHUB_TOKEN or run `gh auth login`.")
    return token


def create_pull_request(owner, repo, token, payload):
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(
        f"{api}/repos/{owner}/{repo}/pulls",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)["html_url"]
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read() or b"{}")
        reasons = "; ".join(err.get("message", str(err)) for err in detail.get("errors", []))
        raise SystemExit(f"GitHub API error {e.code}: {detail.get('message', '')} {reasons}".strip())


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-pr",
        description="Open a GitHub PR from your local changes, with a Claude-written "
                    "description and a Mermaid architecture diagram.")
    parser.add_argument("--base", help="branch to merge into (default: origin's default branch)")
    parser.add_argument("--draft", action="store_true", help="open the PR as a draft")
    parser.add_argument("--dry-run", action="store_true",
                        help="only print the generated PR; don't commit, push or open anything")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # don't crash printing emoji to a non-UTF-8 console

    os.chdir(git("rev-parse", "--show-toplevel"))
    owner, repo = parse_github_repo(git("remote", "get-url", "origin"))
    base = args.base or default_base_branch()
    token = None if args.dry_run else github_token()  # fail before spending an API call

    stat, diff, log = collect_changes(base)
    if not stat:
        raise SystemExit(f"No changes compared to origin/{base}, so there's nothing to open a PR for.")
    print(f"Asking Claude to describe {stat.splitlines()[-1].strip()}...")
    pr = generate_pr_content(base, stat, diff, log)
    body = build_pr_body(pr)

    current = git("rev-parse", "--abbrev-ref", "HEAD")
    branch = unique_branch_name(pr["branch_name"]) if current in (base, "HEAD") else current
    dirty = bool(git("status", "--porcelain"))

    print(f"\n{pr['title']}\n{'=' * len(pr['title'])}\n\n{body}")
    print(f"Branch: {branch} -> {base}")
    if dirty:
        print(f"Commit: {pr['commit_message'].splitlines()[0]}")
    if args.dry_run:
        print("\nDry run: nothing was committed, pushed or opened.")
        return
    if not args.yes:
        try:
            answer = input("\nCommit, push and open this PR? [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            print("Cancelled. Nothing was changed.")
            return

    if branch != current:
        git("checkout", "-b", branch)
    if dirty:
        git("add", "--all")
        git("commit", "-m", pr["commit_message"])
    print(f"Pushing {branch}...")
    git("push", "-u", "origin", branch)
    url = create_pull_request(owner, repo, token, {
        "title": pr["title"], "head": branch, "base": base, "body": body, "draft": args.draft,
    })
    print(f"\nPull request opened: {url}")


if __name__ == "__main__":
    main()
