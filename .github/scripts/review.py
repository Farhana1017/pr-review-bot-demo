"""
Claude PR Review Bot - Main Review Script
Runs inside GitHub Actions, calls Claude API, posts review to PR.

Architecture: Claude returns plain structured text. Python parses it into JSON.
Claude never writes JSON directly — this eliminates all JSON parsing errors.
"""

import os
import sys
import json
import subprocess
import textwrap
import re
import requests
import anthropic

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

CLAUDE_MODEL   = "claude-sonnet-4-5"
MAX_TOKENS     = 4096
MAX_DIFF_CHARS = 15000

# ─────────────────────────────────────────────
# System prompt — plain text output, NOT JSON
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """
You are a senior software engineer performing a code review.
Analyse the provided git diff and respond using EXACTLY the format below — no extra text, no JSON, no markdown fences.

VERDICT: <approve|request_changes|comment>
SCORE: <integer 0-100>
SUMMARY: <one paragraph, no newlines>

ISSUE1_LINE: <integer or null>
ISSUE1_SEVERITY: <critical|warning|style|info>
ISSUE1_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL|Documentation>
ISSUE1_MESSAGE: <one sentence, plain English, no code, no newlines>
ISSUE1_SUGGESTION: <one sentence, plain English, no code, no newlines>

ISSUE2_LINE: <integer or null>
ISSUE2_SEVERITY: <critical|warning|style|info>
ISSUE2_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL|Documentation>
ISSUE2_MESSAGE: <one sentence, plain English, no code, no newlines>
ISSUE2_SUGGESTION: <one sentence, plain English, no code, no newlines>

ISSUE3_LINE: <integer or null>
ISSUE3_SEVERITY: <critical|warning|style|info>
ISSUE3_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL|Documentation>
ISSUE3_MESSAGE: <one sentence, plain English, no code, no newlines>
ISSUE3_SUGGESTION: <one sentence, plain English, no code, no newlines>

ISSUE4_LINE: <integer or null>
ISSUE4_SEVERITY: <critical|warning|style|info>
ISSUE4_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL|Documentation>
ISSUE4_MESSAGE: <one sentence, plain English, no code, no newlines>
ISSUE4_SUGGESTION: <one sentence, plain English, no code, no newlines>

ISSUE5_LINE: <integer or null>
ISSUE5_SEVERITY: <critical|warning|style|info>
ISSUE5_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL|Documentation>
ISSUE5_MESSAGE: <one sentence, plain English, no code, no newlines>
ISSUE5_SUGGESTION: <one sentence, plain English, no code, no newlines>

POSITIVES: <comma separated list of things done well, or "none">
COMMENT: <two sentence GitHub comment summary, no newlines>

Rules:
- Report the 5 most critical issues only. Use fewer if there are fewer real issues.
- Every value must be on a single line. No newlines inside any value.
- No code snippets anywhere. Plain English only.
- Focus on: SQL injection, hardcoded secrets, missing IDisposable, missing DI,
  sync DB/IO calls, SELECT *, missing error handling, missing transactions,
  missing SET NOCOUNT ON, missing BEGIN TRY/CATCH.
"""


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def get_diff() -> str:
    base = os.environ.get("BASE_SHA", "origin/main")
    head = os.environ.get("HEAD_SHA", "HEAD")
    result = subprocess.run(
        ["git", "diff", f"{base}...{head}"],
        capture_output=True, text=True, check=True
    )
    return result.stdout


def detect_language(diff: str) -> str:
    if any(x in diff for x in [".cs\n", ".csproj", "namespace ", "using System"]):
        return "csharp"
    if any(x in diff for x in [".sql\n", "CREATE PROCEDURE", "SELECT ", "INSERT INTO"]):
        return "sql"
    return "general"


def parse_response(text: str) -> dict:
    """
    Parse Claude's plain-text structured response into a dict.
    Claude never writes JSON — we build it here in Python.
    Nothing Claude does can break this parser.
    """
    def get(key: str) -> str:
        match = re.search(rf'^{re.escape(key)}:\s*(.+)$', text, re.MULTILINE)
        return match.group(1).strip() if match else ""

    # Top-level fields
    verdict = get("VERDICT").lower()
    if verdict not in ("approve", "request_changes", "comment"):
        verdict = "comment"

    try:
        score = int(get("SCORE"))
    except ValueError:
        score = 50

    summary = get("SUMMARY")
    comment = get("COMMENT")

    positives_raw = get("POSITIVES")
    positives = (
        []
        if positives_raw.lower() in ("none", "")
        else [p.strip() for p in positives_raw.split(",") if p.strip()]
    )

    # Parse up to 5 issues
    issues = []
    for n in range(1, 6):
        message = get(f"ISSUE{n}_MESSAGE")
        if not message:
            continue  # this slot was not filled
        try:
            line_val = get(f"ISSUE{n}_LINE")
            line = int(line_val) if line_val.lower() != "null" else None
        except ValueError:
            line = None

        issues.append({
            "line":       line,
            "severity":   get(f"ISSUE{n}_SEVERITY") or "info",
            "category":   get(f"ISSUE{n}_CATEGORY") or "Style",
            "message":    message,
            "suggestion": get(f"ISSUE{n}_SUGGESTION"),
        })

    return {
        "summary":        summary,
        "verdict":        verdict,
        "score":          score,
        "issues":         issues,
        "positives":      positives,
        "github_comment": comment,
    }


def build_github_body(review: dict) -> str:
    counts = {"critical": 0, "warning": 0, "style": 0, "info": 0}
    for issue in review["issues"]:
        sev = issue.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1

    table = (
        "| Severity | Count |\n"
        "|----------|-------|\n"
        + "\n".join(f"| {s.capitalize()} | {c} |" for s, c in counts.items())
    )

    issues_md = ""
    for i, iss in enumerate(review["issues"], 1):
        line_info = f" *(line {iss['line']})*" if iss.get("line") else ""
        issues_md += (
            f"\n**{i}. [{iss['severity'].upper()}] {iss['category']}**{line_info}\n"
            f"{iss['message']}\n"
            f"> {iss['suggestion']}\n"
        )

    positives_md = ""
    if review["positives"]:
        positives_md = "\n## ✅ Positives\n" + "\n".join(
            f"- {p}" for p in review["positives"]
        )

    return (
        f"## 🤖 Claude PR Review — Score: {review['score']}/100\n\n"
        f"{table}\n\n"
        f"**Summary:** {review['summary']}\n\n"
        f"## Issues Found\n{issues_md}"
        f"{positives_md}"
    )


def post_github_review(review: dict) -> None:
    token  = os.environ["GITHUB_TOKEN"]
    repo   = os.environ["GITHUB_REPOSITORY"]
    pr_num = os.environ["PR_NUMBER"]

    verdict_map = {
        "approve":         "APPROVE",
        "request_changes": "REQUEST_CHANGES",
        "comment":         "COMMENT",
    }
    event = verdict_map.get(review["verdict"], "COMMENT")
    body  = build_github_body(review)

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/pulls/{pr_num}/reviews",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"body": body, "event": event},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"✅  Review posted — verdict: {event}, score: {review['score']}/100")


def print_local_report(review: dict) -> None:
    divider = "─" * 60
    print(f"\n{divider}")
    print(f"  CLAUDE PR REVIEW  |  Score: {review['score']}/100  |  Verdict: {review['verdict'].upper()}")
    print(divider)
    print(f"\nSUMMARY\n{review['summary']}\n")

    issues = review["issues"]
    if issues:
        print(f"ISSUES ({len(issues)} found)")
        for i, iss in enumerate(issues, 1):
            line_info = f" [line {iss['line']}]" if iss.get("line") else ""
            print(f"\n  {i}. [{iss['severity'].upper()}] {iss['category']}{line_info}")
            print(f"     {iss['message']}")
            if iss.get("suggestion"):
                for ln in textwrap.wrap(iss["suggestion"], 70):
                    print(f"     → {ln}")
    else:
        print("✅  No issues found — clean code!")

    if review["positives"]:
        print("\nPOSITIVES")
        for p in review["positives"]:
            print(f"  ✓ {p}")

    print(f"\n{divider}\nGITHUB COMMENT PREVIEW\n{divider}")
    print(build_github_body(review))
    print(divider)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    print("📂  Extracting diff…")
    try:
        diff = get_diff()
    except subprocess.CalledProcessError as exc:
        print(f"ERROR extracting diff: {exc}", file=sys.stderr)
        sys.exit(1)

    if not diff.strip():
        print("No diff found — nothing to review.")
        sys.exit(0)

    if len(diff) > MAX_DIFF_CHARS:
        print(f"⚠️  Diff truncated to {MAX_DIFF_CHARS} chars (was {len(diff)})")
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated ...]"

    lang = detect_language(diff)
    print(f"🔍  Detected language profile: {lang}")

    print("🤖  Calling Claude API…")
    client   = anthropic.Anthropic(api_key=api_key)
    pr_title = os.environ.get("PR_TITLE", "")
    pr_repo  = os.environ.get("GITHUB_REPOSITORY", "")
    user_msg = f'PR: "{pr_title}" ({pr_repo})\n\nDiff:\n{diff}'

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = message.content[0].text
    print("Raw response:\n", raw)

    review = parse_response(raw)

    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("📬  Posting review to GitHub…")
        try:
            post_github_review(review)
        except requests.HTTPError as exc:
            print(f"ERROR posting to GitHub: {exc}\n{exc.response.text}", file=sys.stderr)
            sys.exit(1)
    else:
        print_local_report(review)

    if review["verdict"] == "request_changes":
        critical_count = sum(
            1 for i in review["issues"] if i.get("severity") == "critical"
        )
        if critical_count > 0:
            print(f"\n❌  {critical_count} critical issue(s) found — failing the check.")
            sys.exit(1)

    print("✅  Review complete.")


if __name__ == "__main__":
    main()
