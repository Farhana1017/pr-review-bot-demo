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
MAX_ISSUES = 8

SYSTEM_PROMPT_CSHARP = """
You are a senior .NET / C# and SQL Server engineer performing a thorough code review.
Analyse the provided git diff and return ONLY a valid JSON object.

ABSOLUTE RULES — the output will be machine-parsed, any violation breaks the pipeline:
1. Output raw JSON only. No markdown fences, no prose, nothing outside the JSON object.
2. The issues array must contain AT MOST 8 entries. Prioritise by severity.
3. Every JSON string value must be a single line. No newline characters of any kind inside strings.
4. suggestion and message values must be plain English sentences only — no code, no backticks, no angle brackets, no special characters.

Focus on: SQL injection, hardcoded secrets, missing IDisposable/using, missing DI, sync DB calls, SELECT *, missing null checks, missing try/catch, SET NOCOUNT ON, BEGIN TRY/CATCH, missing transactions, missing indexes, architecture, XML docs.

Return exactly:
{
  "summary": "plain text overview",
  "verdict": "approve" | "request_changes" | "comment",
  "score": <integer 0-100>,
  "issues": [
    {
      "line": <integer or null>,
      "severity": "critical" | "warning" | "style" | "info",
      "category": "Security" | "Bug" | "Performance" | "Dispose/IDisposable" | "Async" | "DI/IoC" | "Style" | "Error Handling" | "Architecture" | "Documentation" | "T-SQL",
      "message": "plain English, one sentence, no code",
      "suggestion": "plain English, one sentence, no code"
    }
  ],
  "positives": ["plain text"],
  "github_comment": "plain text overview, no code blocks"
}
"""

SYSTEM_PROMPT_SQL = """
You are a senior SQL Server / T-SQL database engineer performing a thorough code review.
Analyse the provided git diff and return ONLY a valid JSON object.

ABSOLUTE RULES — the output will be machine-parsed, any violation breaks the pipeline:
1. Output raw JSON only. No markdown fences, no prose, nothing outside the JSON object.
2. The issues array must contain AT MOST 8 entries. Prioritise by severity.
3. Every JSON string value must be a single line. No newline characters of any kind inside strings.
4. suggestion and message values must be plain English sentences only — no code, no backticks, no angle brackets, no special characters.

Focus on: dynamic SQL concatenation, missing SET NOCOUNT ON, SELECT *, missing indexes, cursors vs set-based, missing transactions, missing BEGIN TRY/CATCH, implicit type conversions, missing schema prefix, missing semicolons.

Return exactly:
{
  "summary": "plain text overview",
  "verdict": "approve" | "request_changes" | "comment",
  "score": <integer 0-100>,
  "issues": [
    {
      "line": <integer or null>,
      "severity": "critical" | "warning" | "style" | "info",
      "category": "Security" | "Performance" | "Missing Index" | "Transaction" | "Error Handling" | "Style" | "Best Practice" | "Dynamic SQL",
      "message": "plain English, one sentence, no code",
      "suggestion": "plain English, one sentence, no code"
    }
  ],
  "positives": ["plain text"],
  "github_comment": "plain text overview, no code blocks"
}
"""

SYSTEM_PROMPT_GENERAL = """
You are a senior software engineer performing a thorough code review.
Analyse the provided git diff and return ONLY a valid JSON object.

ABSOLUTE RULES — the output will be machine-parsed, any violation breaks the pipeline:
1. Output raw JSON only. No markdown fences, no prose, nothing outside the JSON object.
2. The issues array must contain AT MOST 8 entries. Prioritise by severity.
3. Every JSON string value must be a single line. No newline characters of any kind inside strings.
4. suggestion and message values must be plain English sentences only — no code, no backticks, no angle brackets, no special characters.

Focus on: security vulnerabilities, logic errors, performance issues, missing error handling, code style, maintainability, documentation gaps.

Return exactly:
{
  "summary": "plain text overview",
  "verdict": "approve" | "request_changes" | "comment",
  "score": <integer 0-100>,
  "issues": [
    {
      "line": <integer or null>,
      "severity": "critical" | "warning" | "style" | "info",
      "category": "Security" | "Bug" | "Performance" | "Style" | "Error Handling" | "Logic" | "Documentation",
      "message": "plain English, one sentence, no code",
      "suggestion": "plain English, one sentence, no code"
    }
  ],
  "positives": ["plain text"],
  "github_comment": "plain text overview, no code blocks"
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


def _nuke_code_blocks_in_strings(raw: str) -> str:
    """
    The most robust approach: instead of trying to escape newlines inside
    JSON strings, we locate every JSON string value in the raw text and
    flatten it — replacing real newlines with spaces and removing code fences.

    This runs on the raw text BEFORE json.loads so we can fix what Claude broke.
    """
    result = []
    i = 0
    n = len(raw)

    while i < n:
        ch = raw[i]

        # Not inside a string — copy verbatim
        if ch != '"':
            result.append(ch)
            i += 1
            continue

        # Start of a JSON string — collect until closing unescaped quote
        result.append('"')
        i += 1
        string_chars = []

        while i < n:
            c = raw[i]

            if c == '\\' and i + 1 < n:
                # Escaped character — keep as-is
                string_chars.append(c)
                string_chars.append(raw[i + 1])
                i += 2
                continue

            if c == '"':
                # End of string
                i += 1
                break

            # Real (unescaped) control characters inside a string — fix them
            if c == '\n':
                string_chars.append(' ')
            elif c == '\r':
                pass  # drop carriage returns
            elif c == '\t':
                string_chars.append(' ')
            else:
                string_chars.append(c)

            i += 1

        # Now clean up the collected string content:
        # 1. Remove ``` fences and their language tags
        content = ''.join(string_chars)
        content = re.sub(r'```[a-zA-Z]*', '', content)
        content = re.sub(r'```', '', content)
        # 2. Collapse multiple spaces into one
        content = re.sub(r'  +', ' ', content).strip()
        # 3. Escape any remaining double-quotes inside the value
        content = content.replace('"', '\\"')

        result.append(content)
        result.append('"')

    return ''.join(result)


def _repair_truncated_json(s: str) -> str:
    """Close unclosed arrays/objects in a truncated JSON string."""
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
    """Hard-cap issues array to MAX_ISSUES regardless of what Claude returned."""
    if "issues" in review:
        review["issues"] = review["issues"][:MAX_ISSUES]
    return review


def parse_review(raw: str) -> dict:
    """
    Full pipeline:
      1. Strip outer markdown fences
      2. Extract outermost { ... }
      3. Flatten all string values (removes newlines + code fences)
      4. json.loads
      5. Repair if truncated
      6. Enforce limits
    """
    cleaned = raw.strip()

    # Strip outer markdown fences
    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    else:
        cleaned = re.sub(r'```[a-z]*', '', cleaned).replace('```', '').strip()

    # Extract outermost { ... }
    start = cleaned.find("{")
    end   = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in Claude response")

    json_str = cleaned[start:end]

    # THE KEY FIX: flatten string values before parsing
    json_str = _nuke_code_blocks_in_strings(json_str)

    try:
        review = json.loads(json_str)
    except json.JSONDecodeError as exc:
        print(f"⚠️  JSON parse failed ({exc}), attempting repair…", file=sys.stderr)
        repaired = _repair_truncated_json(json_str)
        print(f"⚠️  Repaired (first 200 chars): {repaired[:200]}", file=sys.stderr)
        review = json.loads(repaired)

    return _enforce_limits(review)


def build_summary_table(review: dict) -> str:
    counts = {"critical": 0, "warning": 0, "style": 0, "info": 0}
    for issue in review.get("issues", []):
        sev = issue.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1
    rows = "\n".join(f"| {s.capitalize()} | {c} |" for s, c in counts.items())
    return "| Severity | Count |\n|----------|-------|\n" + rows


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
    score = review.get("score", "N/A")
    body  = (
        f"## Claude PR Review — Score: {score}/100\n\n"
        f"{build_summary_table(review)}\n\n"
        f"{review.get('github_comment', '')}"
    )

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
    print(f"✅  Review posted — verdict: {event}, score: {score}/100")


def print_local_report(review: dict) -> None:
    divider = "─" * 60
    print(f"\n{divider}")
    print(f"  CLAUDE PR REVIEW  |  Score: {review.get('score','N/A')}/100  |  Verdict: {review.get('verdict','').upper()}")
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

    if review.get("positives"):
        print("\nPOSITIVES")
        for p in review["positives"]:
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
