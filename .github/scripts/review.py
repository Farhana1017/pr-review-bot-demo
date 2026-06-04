"""
Claude PR Review Bot - Main Review Script
Runs inside GitHub Actions, calls Claude API, posts review to PR.
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

CLAUDE_MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 8096
MAX_DIFF_CHARS = 15000
MAX_ISSUES = 10          # hard cap enforced in code, not just prompt

SYSTEM_PROMPT_CSHARP = """
You are a senior .NET / C# and SQL Server engineer performing a thorough code review.
Analyse the provided git diff and return ONLY a valid JSON object.

CRITICAL JSON RULES:
1. Return ONLY raw JSON. No markdown fences, no ```json, no text before or after the JSON.
2. No literal newlines inside any string value. Use the two characters backslash-n to represent a newline.
3. No code blocks inside JSON strings. Write code examples as plain text with backslash-n for line breaks.
4. Return AT MOST 10 issues. Choose only the most critical ones.
5. Keep suggestion fields short — one or two sentences maximum. No multi-line code samples.

Focus on: SQL injection, hardcoded secrets, missing IDisposable/using, missing DI, sync DB calls, SELECT *, missing null checks, missing try/catch, missing SET NOCOUNT ON, missing BEGIN TRY/CATCH, missing transactions, missing indexes, architecture violations, missing XML docs.

Return exactly this JSON structure with no deviations:
{
  "summary": "2-3 sentence overview",
  "verdict": "approve" | "request_changes" | "comment",
  "score": <integer 0-100>,
  "issues": [
    {
      "line": <integer or null>,
      "severity": "critical" | "warning" | "style" | "info",
      "category": "Security" | "Bug" | "Performance" | "Dispose/IDisposable" | "Async" | "DI/IoC" | "Style" | "Error Handling" | "Architecture" | "Documentation" | "T-SQL",
      "message": "one sentence description",
      "suggestion": "one sentence fix, no code blocks"
    }
  ],
  "positives": ["one sentence each"],
  "github_comment": "markdown summary with backslash-n for line breaks, no raw newlines"
}
"""

SYSTEM_PROMPT_SQL = """
You are a senior SQL Server / T-SQL database engineer performing a thorough code review.
Analyse the provided git diff and return ONLY a valid JSON object.

CRITICAL JSON RULES:
1. Return ONLY raw JSON. No markdown fences, no ```json, no text before or after the JSON.
2. No literal newlines inside any string value. Use the two characters backslash-n to represent a newline.
3. No code blocks inside JSON strings. Write code examples as plain text with backslash-n for line breaks.
4. Return AT MOST 10 issues. Choose only the most critical ones.
5. Keep suggestion fields short — one or two sentences maximum. No multi-line code samples.

Focus on: dynamic SQL concatenation, missing SET NOCOUNT ON, SELECT *, missing indexes, cursors vs set-based, missing transactions, missing BEGIN TRY/CATCH, implicit type conversions, missing schema prefix, missing semicolons.

Return exactly this JSON structure with no deviations:
{
  "summary": "2-3 sentence overview",
  "verdict": "approve" | "request_changes" | "comment",
  "score": <integer 0-100>,
  "issues": [
    {
      "line": <integer or null>,
      "severity": "critical" | "warning" | "style" | "info",
      "category": "Security" | "Performance" | "Missing Index" | "Transaction" | "Error Handling" | "Style" | "Best Practice" | "Dynamic SQL",
      "message": "one sentence description",
      "suggestion": "one sentence fix, no code blocks"
    }
  ],
  "positives": ["one sentence each"],
  "github_comment": "markdown summary with backslash-n for line breaks, no raw newlines"
}
"""

SYSTEM_PROMPT_GENERAL = """
You are a senior software engineer performing a thorough code review.
Analyse the provided git diff and return ONLY a valid JSON object.

CRITICAL JSON RULES:
1. Return ONLY raw JSON. No markdown fences, no ```json, no text before or after the JSON.
2. No literal newlines inside any string value. Use the two characters backslash-n to represent a newline.
3. No code blocks inside JSON strings. Write code examples as plain text with backslash-n for line breaks.
4. Return AT MOST 10 issues. Choose only the most critical ones.
5. Keep suggestion fields short — one or two sentences maximum. No multi-line code samples.

Focus on: security vulnerabilities, logic errors, performance issues, missing error handling, code style, maintainability, documentation gaps.

Return exactly this JSON structure with no deviations:
{
  "summary": "2-3 sentence overview",
  "verdict": "approve" | "request_changes" | "comment",
  "score": <integer 0-100>,
  "issues": [
    {
      "line": <integer or null>,
      "severity": "critical" | "warning" | "style" | "info",
      "category": "Security" | "Bug" | "Performance" | "Style" | "Error Handling" | "Logic" | "Documentation",
      "message": "one sentence description",
      "suggestion": "one sentence fix, no code blocks"
    }
  ],
  "positives": ["one sentence each"],
  "github_comment": "markdown summary with backslash-n for line breaks, no raw newlines"
}
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


def _strip_code_fences(s: str) -> str:
    """Remove any ``` code fences that Claude snuck into string values."""
    # Remove ```language...``` blocks entirely, keeping only the inner text
    s = re.sub(r'```[a-z]*\n?', '', s)
    s = re.sub(r'```', '', s)
    return s


def _sanitize_json_strings(s: str) -> str:
    """
    Walk char-by-char and escape literal control characters inside JSON strings.
    Also auto-closes an unclosed string if the response was truncated.
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
            elif ord(ch) < 0x20:
                result.append(f'\\u{ord(ch):04x}')
                continue
        result.append(ch)

    if in_string:
        result.append('"')

    return ''.join(result)


def _repair_truncated_json(s: str) -> str:
    """Close any unclosed arrays/objects in a truncated JSON string."""
    def count_open(text):
        open_braces = open_brackets = 0
        in_str = esc = False
        for ch in text:
            if esc:
                esc = False
                continue
            if ch == '\\' and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':    open_braces += 1
            elif ch == '}':  open_braces -= 1
            elif ch == '[':  open_brackets += 1
            elif ch == ']':  open_brackets -= 1
        return open_braces, open_brackets

    trimmed = s.rstrip()
    last_good = 0
    in_str = esc = False
    depth = 0

    for i, ch in enumerate(trimmed):
        if esc:
            esc = False
            continue
        if ch == '\\' and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            if not in_str:
                last_good = i + 1
            continue
        if in_str:
            continue
        if ch in ('{', '['):
            depth += 1
        elif ch in ('}', ']'):
            depth -= 1
            last_good = i + 1
        elif ch == ',' and depth <= 2:
            last_good = i

    cut = trimmed[:last_good].rstrip().rstrip(',')
    ob, obr = count_open(cut)
    return cut + ']' * max(0, obr) + '}' * max(0, ob)


def _enforce_limits(review: dict) -> dict:
    """
    Hard-cap issues to MAX_ISSUES and strip any leftover code fences
    from string fields — enforced in code regardless of what Claude returned.
    """
    if "issues" in review:
        review["issues"] = review["issues"][:MAX_ISSUES]

    # Strip code fences from suggestion and message fields
    for issue in review.get("issues", []):
        for field in ("suggestion", "message"):
            if isinstance(issue.get(field), str):
                issue[field] = _strip_code_fences(issue[field]).strip()

    # Strip code fences from github_comment too
    if isinstance(review.get("github_comment"), str):
        review["github_comment"] = _strip_code_fences(review["github_comment"]).strip()

    return review


def parse_review(raw: str) -> dict:
    """Full pipeline: strip fences → extract JSON → sanitize → parse → repair → enforce limits."""
    cleaned = raw.strip()

    # Strip outer markdown fences
    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    else:
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    # Extract outermost { ... }
    start = cleaned.find("{")
    end   = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in Claude response")

    json_str = cleaned[start:end]
    json_str = _sanitize_json_strings(json_str)

    try:
        review = json.loads(json_str)
    except json.JSONDecodeError as exc:
        print(f"⚠️  JSON parse failed ({exc}), attempting repair…", file=sys.stderr)
        repaired = _repair_truncated_json(json_str)
        print(f"⚠️  Repaired JSON (first 200 chars): {repaired[:200]}", file=sys.stderr)
        review = json.loads(repaired)

    return _enforce_limits(review)


def build_summary_table(review: dict) -> str:
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
    token  = os.environ["GITHUB_TOKEN"]
    repo   = os.environ["GITHUB_REPOSITORY"]
    pr_num = os.environ["PR_NUMBER"]

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
        print("\nPOSITIVES")
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

    lang          = detect_language(diff)
    system_prompt = pick_system_prompt(lang)
    print(f"🔍  Detected language profile: {lang}")

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

    try:
        review = parse_review(raw_response)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR parsing Claude response: {exc}", file=sys.stderr)
        print(f"Response length: {len(raw_response)} chars", file=sys.stderr)
        sys.exit(1)

    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("📬  Posting review to GitHub…")
        try:
            post_github_review(review)
        except requests.HTTPError as exc:
            print(f"ERROR posting to GitHub: {exc}\n{exc.response.text}", file=sys.stderr)
            sys.exit(1)
    else:
        print_local_report(review)

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
