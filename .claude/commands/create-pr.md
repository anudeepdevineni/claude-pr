---
description: Commit my local changes and open a GitHub PR with a Mermaid diagram
argument-hint: "[base-branch]"
allowed-tools: Bash(git:*), Bash(gh:*)
---

Open a GitHub pull request for my local changes.

## Context
- Current branch: !`git branch --show-current`
- Changed files: !`git status --short`
- Default branch: !`gh repo view --json defaultBranchRef -q .defaultBranchRef.name`
- Requested base branch (empty = default branch): $ARGUMENTS

## Steps
1. Read the whole change: uncommitted and untracked work (`git diff HEAD`, new files from
   `git status`) plus commits not yet on the base branch (`git log origin/<base>..HEAD`,
   `git diff origin/<base>...HEAD`).
2. If I'm on the base branch, create a branch like `feat/<short-name>` or `fix/<short-name>`.
   Stage everything and commit with a Conventional Commits message.
3. Push with `git push -u origin <branch>`.
4. Write the PR body to a temp file and run
   `gh pr create --base <base> --title "<title>" --body-file <file>`. Title: imperative, under
   70 characters. Body sections:
   - `## Summary`: 2-4 sentences on what changed and why.
   - `## Architecture`: a ```` ```mermaid ```` block (`flowchart TD`, or `sequenceDiagram` for
     interactions over time, 5-15 nodes) showing how the changed components connect. Quote every
     node label, e.g. `A["parse_config()"]`, so it renders on GitHub.
   - `## Changes`: one bullet per meaningful change, naming files.
   - `## How to test`: a `- [ ]` checklist a reviewer can follow.
5. Reply with the PR URL.

Describe only what the diff shows.
