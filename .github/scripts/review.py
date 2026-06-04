"""
Claude PR Review Bot - Main Review Script
Runs inside GitHub Actions, calls Claude API, posts review to PR.
"""

import os
import sys
import json
import subprocess
import textwrap
import requests
import anthropic

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

CLAUDE_MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 8096        # Sonnet 4.5 real output ceiling is 8192; leave a small buffer
MAX_DIFF_CHARS = 15000   # trim huge diffs so we stay within context limits

SYSTEM_PROMPT_CSHARP = """
You are a senior .NET / C# and SQL Server engineer performing a thorough code review.
Analyse the provided git diff and return ONLY a valid JSON object — no markdown fences, no explanation outside the JSON.

CRITICAL JSON RULES - YOU MUST FOLLOW THESE:
1. Return ONLY raw JSON. No markdown fences, no ```json, no explanation.
2. Every string value MUST be on a single line - absolutely no literal newlines inside strings.
3. In 'suggestion' fields: escape ALL newlines as \\n, ALL double-quotes as \\", ALL backslashes as \\\\.
4. Code examples in suggestions must be written as a single line with \\n for line breaks.
5. Do not include raw code blocks inside JSON strings.

Focus especially on:
- SQL injection via string concatenation (must use parameterised queries / sp_executesql)
- Hardcoded connection strings or secrets (use IConfiguration / Secret Manager / environment variables)
- Missing using / IDisposable patterns (SqlConnection, SqlCommand, HttpClient must be disposed)
- Missing dependency injection (do not new-up services directly; inject via constructor)
- Synchronous DB / IO calls that should be async/await
- SELECT * usage instead of projecting only needed columns
- Missing null checks, missing try/catch around external calls
- Missing SET NOCOUNT ON in stored procedures
- Missing BEGIN TRY / BEGIN CATCH in T-SQL
- Missing transactions (BEGIN TRAN / COMMIT / ROLLBACK) for multi-statement DML
- Dynamic SQL built with string concatenation instead of sp_executesql with parameters
- Missing indexes on JOIN / WHERE columns
- Violation of repository/service pattern separation
- Missing XML doc comments on public API surface

Return exactly this JSON structure:
{
  "summary": "2-3 sentence overview of the changes and overall quality",
  "verdict": "approve" | "request_changes" | "comment",
  "score": <integer 0-100>,
  "issues": [
    {
      "line": <integer line number in the new file, or null>,
      "severity": "critical" | "warning" | "style" | "info",
      "category": "Security" | "Bug" | "Performance" | "Dispose/IDisposable" | "Async" | "DI/IoC" | "Style" | "Error Handling" | "Architecture" | "Documentation" | "T-SQL",
      "message": "Clear description of the issue",
      "suggestion": "Concrete fix with example code where helpful - use \\n for line breaks in code"
    }
  ],
  "positives": ["things done well"],
  "github_comment": "Full markdown-formatted comment for GitHub PR (use headings, code blocks, tables) - use \\n for line breaks"
}
"""

SYSTEM_PROMPT_SQL = """
You are a senior SQL Server / T-SQL database engineer performing a thorough code review.
Analyse the provided git diff and return ONLY a valid JSON object — no markdown fences, no explanation outside the JSON.

CRITICAL JSON RULES - YOU MUST FOLLOW THESE:
1. Return ONLY raw JSON. No markdown fences, no ```json, no explanation.
2. Every string value MUST be on a single line - absolutely no literal newlines inside strings.
3. In 'suggestion' fields: escape ALL newlines as \\n, ALL double-quotes as \\", ALL backslashes as \\\\.
4. Code examples in suggestions must be written as a single line with \\n for line breaks.
5. Do not include raw code blocks inside JSON strings.

Focus especially on:
- Dynamic SQL built with string concatenation (sp_executesql with parameters is the correct pattern)
- Missing SET NOCOUNT ON in stored procedures (causes extra network round-trips)
- SELECT * usage (always project explicit columns)
- Missing indexes on JOIN and WHERE columns
- N+1 query patterns or cursor usage instead of set-based operations
- Missing transactions (BEGIN TRAN / COMMIT / ROLLBACK) for multi-statement DML
- Missing error handling (BEGIN TRY / BEGIN CATCH)
- Implicit type conversions causing index scans instead of seeks
- Unparameterised dynamic SQL enabling SQL injection
- Missing schema prefix (dbo.) on object references
- Missing semicolons between statements

Return exactly this JSON structure:
{
  "summary": "2-3 sentence overview of the changes and overall quality",
  "verdict": "approve" | "request_changes" | "comment",
  "score": <integer 0-100>,
  "issues": [
    {
      "line": <integer line number in the new file, or null>,
      "severity": "critical" | "warning" | "style" | "info",
      "category": "Security" | "Performance" | "Missing Index" | "Transaction" | "Error Handling" | "Style" | "Best Practice" | "Dynamic SQL",
      "message": "Clear description of the T-SQL-specific issue",
      "suggestion": "Concrete T-SQL fix or pattern - use \\n for line breaks in code"
    }
  ],
  "positives": ["things done well"],
  "github_comment": "Full markdown-formatted comment for GitHub PR (use headings, code blocks, tables) - use \\n for line breaks"
}
"""

SYSTEM_PROMPT_GENERAL = """
You are a senior software engineer performing a thorough code review.
Analyse the provided git diff and return ONLY a valid JSON object — no markdown fences, no explanation outside the JSON.

CRITICAL JSON RULES - YOU MUST FOLLOW THESE:
1. Return ONLY raw JSON. No markdown fences, no ```json, no explanation.
2. Every string value MUST be on a single line - absolutely no literal newlines inside strings.
3. In 'suggestion' fields: escape ALL newlines as \\n, ALL double-quotes as \\", ALL backslashes as \\\\.
4. Code examples in suggestions must be written as a single line with \\n for line breaks.
5. Do not include raw code blocks inside JSON strings.

Focus on: security vulnerabilities, logic errors, performance issues, missing error handling,
code style, maintainability, and documentation gaps.

Return exactly this JSON structure:
{
  "summary": "2-3 sentence overview of the changes and overall quality",
  "verdict": "approve" | "request_changes" | "comment",
  "score": <integer 0-100>,
  "issues": [
    {
      "line": <integer line number in the new file, or null>,
      "severity": "critical" | "warning" | "style" | "info",
      "category": "Security" | "Bug" | "Performance" | "Style" | "Error Handling" | "Logic" | "Documentation",
      "message": "Clear description of the issue",
      "suggestion": "Concrete fix or improvement - use \\n for line breaks in code"
    }
  ],
  "positives": ["things done well"],
  "github_comment": "Full markdown-formatted comment for GitHub PR (use headings, code blocks, tables) - use \\n for line breaks"
}
"""


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def get_diff() -> str:
    """Extract the git diff for this PR."""
    base = os.environ.get("BASE_SHA", "origin/main")
    head = os.environ.get("HEAD_SHA", "HEAD")
    result = subprocess.run(
        ["git", "diff", f"{base}...{head}"],
        capture_output=True, text=True, check=True
    )
    return result.stdout


def detect_language(diff: str) -> str:
    """Guess the primary language from file extensions in the diff."""
    if any(ext in diff for ext in [".cs\n", ".csproj", ".sln", "namespace ", "using System"]):
        return "csharp"
    if any(ext in diff for ext in [".sql\n", "CREATE PROCEDURE", "CREATE TABLE",
                                    "SELECT ", "INSERT INTO", "BEGIN TRAN"]):
        return "sql"
    return "general"


def pick_system_prompt(lang: str) -> str:
    return {
        "csharp": SYSTEM_PROMPT_CSHARP,
        "sql":    SYSTEM_PROMPT_SQL,
    }.get(lang, SYSTEM_PROMPT_GENERAL)


def _sanitize_json_strings(s: str) -> str:
    """
    Replace literal control characters inside JSON string values with proper
    JSON escape sequences. Also auto-closes an unclosed string if the response
    was truncated mid-value.
    """
    result = []
    in_string = False
    escape_next = False

    for ch in s:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string:
            if ch == '\n':
                result.append('\\n')
                continue
            elif ch == '\r':
                result.append('\\r')
                continue
            elif ch == '\t':
                result.append('\\t')
                continue
            elif ord(ch) < 0x20:          # catch ALL other control characters
                result.append(f'\\u{ord(ch):04x}')
                continue
        result.append(ch)

    # If response was truncated mid-string, close it gracefully
    if in_string:
        result.append('"')

    return ''.join(result)


def parse_review(raw: str) -> dict:
    """Parse JSON from Claude's response, stripping any accidental fences."""
    import re
    cleaned = raw.strip()

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    else:
        for fence in ("```json", "```"):
            cleaned = cleaned.replace(fence, "")
        cleaned = cleaned.strip()

    # Find the outermost { ... }
    start = cleaned.find("{")
    end   = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in Claude response")

    json_str = cleaned[start:end]

    # Sanitize literal control characters inside JSON string values
    json_str = _sanitize_json_strings(json_str)

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as exc:
        print(f"⚠️  JSON parse failed ({exc}), attempting recovery…", file=sys.stderr)
        recovered = _repair_truncated_json(json_str)
        print(f"⚠️  Recovered JSON (first 200 chars): {recovered[:200]}", file=sys.stderr)
        return json.loads(recovered)


def _repair_truncated_json(s: str) -> str:
    """
    Best-effort repair of a truncated JSON string by:
    1. Removing any incomplete trailing object or string value.
    2. Closing any unclosed arrays and objects.
    """
    def count_open(text):
        open_braces = 0
        open_brackets = 0
        in_string = False
        escape = False
        for ch in text:
            if escape:
                escape = False
                continue
            if ch == '\\' and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                open_braces += 1
            elif ch == '}':
                open_braces -= 1
            elif ch == '[':
                open_brackets += 1
            elif ch == ']':
                open_brackets -= 1
        return open_braces, open_brackets

    trimmed = s.rstrip()
    last_complete_pos = 0
    in_string = False
    escape_next = False
    depth = 0

    for i, ch in enumerate(trimmed):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            if not in_string:
                last_complete_pos = i + 1
            continue
        if in_string:
            continue
        if ch in ('{', '['):
            depth += 1
        elif ch in ('}', ']'):
            depth -= 1
            last_complete_pos = i + 1
        elif ch == ',' and depth <= 2:
            last_complete_pos = i

    cut = trimmed[:last_complete_pos].rstrip().rstrip(',')
    open_braces, open_brackets = count_open(cut)
    closing = ']' * max(0, open_brackets) + '}' * max(0, open_braces)
    return cut + closing


def build_summary_table(review: dict) -> str:
    """Build a markdown severity-count table for the PR comment header."""
    counts = {"critical": 0, "warning": 0, "style": 0, "info": 0}
    for issue in review.get("issues", []):
        sev = issue.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1

    rows = "\n".join(
        f"| {sev.capitalize()} | {count} |"
        for sev, count in counts.items()
    )
    return (
        "| Severity | Count |\n"
        "|----------|-------|\n"
        f"{rows}"
    )


def post_github_review(review: dict) -> None:
    """Post the review to GitHub via the REST API."""
    token   = os.environ["GITHUB_TOKEN"]
    repo    = os.environ["GITHUB_REPOSITORY"]
    pr_num  = os.environ["PR_NUMBER"]

    verdict_map = {
        "approve":         "APPROVE",
        "request_changes": "REQUEST_CHANGES",
        "comment":         "COMMENT",
    }
    event = verdict_map.get(review.get("verdict", "comment"), "COMMENT")

    score   = review.get("score", "N/A")
    summary = build_summary_table(review)
    body    = (
        f"## Claude PR Review — Score: {score}/100\n\n"
        f"{summary}\n\n"
        f"{review.get('github_comment', '')}"
    )

    url  = f"https://api.github.com/repos/{repo}/pulls/{pr_num}/reviews"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"body": body, "event": event},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"✅  Review posted — verdict: {event}, score: {score}/100")


def print_local_report(review: dict) -> None:
    """Pretty-print the review when running locally (no GitHub env vars)."""
    divider = "─" * 60
    print(f"\n{divider}")
    print(f"  CLAUDE PR REVIEW  |  Score: {review.get('score', 'N/A')}/100  |  Verdict: {review.get('verdict','').upper()}")
    print(divider)
    print(f"\nSUMMARY\n{review.get('summary','')}\n")

    issues = review.get("issues", [])
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

    positives = review.get("positives", [])
    if positives:
        print(f"\nPOSITIVES")
        for p in positives:
            print(f"  ✓ {p}")

    print(f"\n{divider}\nGITHUB COMMENT PREVIEW\n{divider}")
    print(review.get("github_comment", ""))
    print(divider)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    # ── 1. Get diff ──────────────────────────
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
        print(f"⚠️   Diff truncated to {MAX_DIFF_CHARS} chars (was {len(diff)})")
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated ...]"

    # ── 2. Pick prompt based on language ─────
    lang          = detect_language(diff)
    system_prompt = pick_system_prompt(lang)
    print(f"🔍  Detected language profile: {lang}")

    # ── 3. Call Claude ───────────────────────
    print("🤖  Calling Claude API…")
    client = anthropic.Anthropic(api_key=api_key)

    pr_title = os.environ.get("PR_TITLE", "")
    pr_repo  = os.environ.get("GITHUB_REPOSITORY", "")
    user_msg = f'PR: "{pr_title}" ({pr_repo})\n\nDiff:\n{diff}'

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw_response = message.content[0].text
    print("Raw response:\n", raw_response)

    # ── 4. Parse response ────────────────────
    try:
        review = parse_review(raw_response)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR parsing Claude response: {exc}", file=sys.stderr)
        print(f"Response length: {len(raw_response)} chars", file=sys.stderr)
        sys.exit(1)

    # ── 5. Post or print ─────────────────────
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("📬  Posting review to GitHub…")
        try:
            post_github_review(review)
        except requests.HTTPError as exc:
            print(f"ERROR posting to GitHub: {exc}\n{exc.response.text}", file=sys.stderr)
            sys.exit(1)
    else:
        print_local_report(review)

    # Exit with non-zero if critical issues exist and verdict is request_changes
    if review.get("verdict") == "request_changes":
        critical_count = sum(
            1 for i in review.get("issues", []) if i.get("severity") == "critical"
        )
        if critical_count > 0:
            print(f"\n❌  {critical_count} critical issue(s) found — failing the check.")
            sys.exit(1)

    print("✅  Review complete.")


if __name__ == "__main__":
    main()
