#!/usr/bin/env python3
"""AlgoTrader PR review team.

Runs a small team of role-based reviewers over a pull request's diff and posts a
single combined review comment. Three specialist bots (QA, Financial Strategy,
Trader) review the diff in parallel-of-concern; a Project Manager bot then
synthesizes their findings and gives the combined ship/hold call.

Design goals:
- Self-contained: only depends on the `anthropic` SDK plus the standard library
  (GitHub API calls go through urllib). No app dependencies, so it can't break
  or be broken by the trading bot's own requirements.
- Fails safe: if there is no API key, no diff, or any single bot errors, it
  degrades gracefully and never fails the PR check.
- Versioned prompts: each bot's persona lives in .claude/review-team/*.md so the
  review criteria are reviewable and editable like any other code.

Environment:
  ANTHROPIC_API_KEY   required to run; if unset the script exits 0 with a notice.
  GITHUB_TOKEN        required to post the comment (auto-provided in Actions).
  GITHUB_REPOSITORY   "owner/repo" (auto in Actions).
  PR_NUMBER           pull request number (the workflow passes this).
  GITHUB_BASE_REF     base branch name (auto in Actions on pull_request).
  REVIEW_MODEL        Claude model id (default claude-opus-5; e.g. claude-sonnet-5 to cut cost).
  REVIEW_EFFORT       low|medium|high|xhigh|max (default high).
  REVIEW_MAX_DIFF_CHARS  cap on diff size sent to the model (default 120000).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MARKER = "<!-- algotrader-review-team -->"
PROMPT_DIR = Path(__file__).resolve().parent.parent / ".claude" / "review-team"

# (file, display name, emoji). QA/Strategy/Trader run first; PM runs last.
SPECIALISTS = [
    ("qa.md", "QA Bot", "🧪"),
    ("financial-strategy.md", "Financial Strategy Bot", "📈"),
    ("trader.md", "Trader Bot", "💹"),
]
COORDINATOR = ("project-manager.md", "Project Manager Bot", "📋")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def run_git_diff(base_ref: str) -> tuple[str, str]:
    """Return (unified diff, changed-file list) for the PR against its base."""
    base = base_ref or "main"
    # Make sure the base branch is available locally, then diff the merge base.
    subprocess.run(["git", "fetch", "--quiet", "origin", base], check=False)
    merge_base = subprocess.run(
        ["git", "merge-base", f"origin/{base}", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip() or f"origin/{base}"
    diff = subprocess.run(
        ["git", "diff", "--no-color", f"{merge_base}...HEAD"],
        capture_output=True, text=True,
    ).stdout
    files = subprocess.run(
        ["git", "diff", "--name-status", f"{merge_base}...HEAD"],
        capture_output=True, text=True,
    ).stdout
    return diff, files


def build_shared_context(pr_title: str, pr_body: str, files: str, diff: str) -> str:
    return (
        "You are one bot on an automated pull-request review team for AlgoTrader, "
        "an autonomous PAPER-trading bot (Alpaca paper account, Alpaca IEX market "
        "data). The bot must stay paper-only and safe while it works toward a real, "
        "measured trading edge. Review ONLY the change below.\n\n"
        f"## PR title\n{pr_title or '(none)'}\n\n"
        f"## PR description\n{pr_body or '(none)'}\n\n"
        f"## Changed files\n{files or '(none)'}\n\n"
        f"## Unified diff\n```diff\n{diff}\n```\n"
    )


def call_claude(client, model: str, effort: str, shared_context: str,
                role_prompt: str, user_instruction: str, max_tokens: int) -> str:
    """One reviewer pass. Diff lives in a cached system block so the 4 calls
    reuse it cheaply; the role prompt and instruction vary per call."""
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        system=[
            {"type": "text", "text": shared_context,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": role_prompt},
        ],
        messages=[{"role": "user", "content": user_instruction}],
    ) as stream:
        message = stream.get_final_message()
    return "".join(b.text for b in message.content if b.type == "text").strip()


def read_prompt(filename: str) -> str:
    return (PROMPT_DIR / filename).read_text(encoding="utf-8")


def github_request(method: str, url: str, token: str, payload: dict | None = None) -> dict | list:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:  # noqa: S310 (trusted GitHub API)
        body = resp.read().decode()
    return json.loads(body) if body else {}


def upsert_comment(repo: str, pr_number: str, token: str, body: str) -> None:
    """Update the team's existing comment in place, else create it."""
    base = f"https://api.github.com/repos/{repo}"
    existing_id = None
    try:
        comments = github_request(
            "GET", f"{base}/issues/{pr_number}/comments?per_page=100", token)
        for c in comments if isinstance(comments, list) else []:
            if MARKER in (c.get("body") or ""):
                existing_id = c.get("id")
                break
    except urllib.error.HTTPError as exc:  # listing failed; fall through to create
        print(f"::warning::could not list PR comments: {exc}")
    if existing_id:
        github_request("PATCH", f"{base}/issues/comments/{existing_id}", token, {"body": body})
        print(f"Updated review comment {existing_id}")
    else:
        github_request("POST", f"{base}/issues/{pr_number}/comments", token, {"body": body})
        print("Created review comment")


def main() -> int:
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        print("::notice::ANTHROPIC_API_KEY not set — review team skipped. "
              "Add the repo secret to enable automated PR reviews.")
        return 0

    repo = _env("GITHUB_REPOSITORY")
    pr_number = _env("PR_NUMBER")
    gh_token = _env("GITHUB_TOKEN")
    base_ref = _env("GITHUB_BASE_REF")
    model = _env("REVIEW_MODEL", "claude-opus-5")
    effort = _env("REVIEW_EFFORT", "high")
    max_diff = int(_env("REVIEW_MAX_DIFF_CHARS", "120000") or "120000")

    diff, files = run_git_diff(base_ref)
    if not diff.strip():
        print("::notice::empty diff — nothing to review.")
        return 0
    truncated = ""
    if len(diff) > max_diff:
        diff = diff[:max_diff]
        truncated = ("\n\n> ⚠️ Diff truncated for review — only the first "
                     f"{max_diff} characters were analysed.")

    pr_title = _env("PR_TITLE")
    pr_body = _env("PR_BODY")

    try:
        import anthropic
    except ImportError:
        print("::error::anthropic SDK not installed.")
        return 0  # never fail the PR check on our own tooling gap

    client = anthropic.Anthropic(api_key=api_key)
    shared = build_shared_context(pr_title, pr_body, files, diff) + truncated

    sections: list[str] = []
    reviews_for_pm: list[str] = []
    for filename, name, emoji in SPECIALISTS:
        try:
            out = call_claude(
                client, model, effort, shared, read_prompt(filename),
                "Review this PR strictly from your role. Be concise and specific.",
                max_tokens=6000,
            )
        except Exception as exc:  # one bot failing must not sink the review
            out = f"_({name} could not complete: {exc})_"
            print(f"::warning::{name} failed: {exc}")
        sections.append(f"### {emoji} {name}\n\n{out}")
        reviews_for_pm.append(f"## {name}\n{out}")

    # Coordinator synthesizes the three specialist reviews.
    pm_file, pm_name, pm_emoji = COORDINATOR
    pm_instruction = (
        "Synthesize the review team and give the combined call. Here are the three "
        "specialist reviews:\n\n" + "\n\n".join(reviews_for_pm)
    )
    try:
        pm_out = call_claude(client, model, effort, shared, read_prompt(pm_file),
                             pm_instruction, max_tokens=3000)
    except Exception as exc:
        pm_out = f"_({pm_name} could not complete: {exc})_"
        print(f"::warning::{pm_name} failed: {exc}")

    body = (
        f"{MARKER}\n"
        f"## 🤖 AlgoTrader Review Team\n\n"
        f"### {pm_emoji} {pm_name} — coordinator\n\n{pm_out}\n\n"
        f"<details><summary>Specialist reviews (QA · Strategy · Trader)</summary>\n\n"
        + "\n\n".join(sections)
        + "\n\n</details>\n\n"
        f"---\n_Automated review by the Claude review team "
        f"(model: `{model}`, effort: `{effort}`). Advisory only — not a merge gate._"
    )

    if repo and pr_number and gh_token:
        upsert_comment(repo, pr_number, gh_token, body)
    else:
        print("::notice::missing GITHUB_REPOSITORY/PR_NUMBER/GITHUB_TOKEN — printing review:\n")
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
