"""
Claude PR Review Bot
Claude returns plain structured text. Python builds the dict.
Claude never writes JSON — cannot produce JSON parse errors.
"""

import os
import sys
import subprocess
import textwrap
import re
import requests
import anthropic

CLAUDE_MODEL   = "claude-sonnet-4-5"
MAX_TOKENS     = 4096
MAX_DIFF_CHARS = 15000

SYSTEM_PROMPT = """
You are a senior software engineer performing a code review.
Respond using EXACTLY this format. Nothing else. No JSON. No markdown. No extra text.

VERDICT: <approve|request_changes|comment>
SCORE: <integer 0-100>
SUMMARY: <one sentence>
ISSUE1_LINE: <integer or null>
ISSUE1_SEVERITY: <critical|warning|style|info>
ISSUE1_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL>
ISSUE1_MESSAGE: <one sentence, no code>
ISSUE1_SUGGESTION: <one sentence, no code>
ISSUE2_LINE: <integer or null>
ISSUE2_SEVERITY: <critical|warning|style|info>
ISSUE2_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL>
ISSUE2_MESSAGE: <one sentence, no code>
ISSUE2_SUGGESTION: <one sentence, no code>
ISSUE3_LINE: <integer or null>
ISSUE3_SEVERITY: <critical|warning|style|info>
ISSUE3_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL>
ISSUE3_MESSAGE: <one sentence, no code>
ISSUE3_SUGGESTION: <one sentence, no code>
ISSUE4_LINE: <integer or null>
ISSUE4_SEVERITY: <critical|warning|style|info>
ISSUE4_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL>
ISSUE4_MESSAGE: <one sentence, no code>
ISSUE4_SUGGESTION: <one sentence, no code>
ISSUE5_LINE: <integer or null>
ISSUE5_SEVERITY: <critical|warning|style|info>
ISSUE5_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL>
ISSUE5_MESSAGE: <one sentence, no code>
ISSUE5_SUGGESTION: <one sentence, no code>
POSITIVES: <comma separated or none>
COMMENT: <one sentence>

Rules: max 5 issues, no newlines in any value, no code, no backticks anywhere.
"""


def get_diff() -> str:
    base = os.environ.get("BASE_SHA", "origin/main")
    head = os.environ.get("HEAD_SHA", "HEAD")
    result = subprocess.run(
        ["git", "diff", f"{base}...{head}"],
        capture_output=True, text=True, check=True
    )
    return result.stdout


def parse_response(text: str) -> dict:
    def get(key: str) -> str:
        match = re.search(rf'^{re.escape(key)}:\s*(.+)$', text, re.MULTILINE)
        return match.group(1).strip() if match else ""

    verdict = get("VERDICT").lower()
    if verdict not in ("approve", "request_changes", "comment"):
        verdict = "comment"

    try:
        score = int(get("SCORE"))
    except ValueError:
        score = 50

    positives_raw = get("POSITIVES")
    positives = (
        [] if positives_raw.lower() in ("none", "")
        else [p.strip() for p in positives_raw.split(",") if p.strip()]
    )

    issues = []
    for n in range(1, 6):
        message = get(f"ISSUE{n}_MESSAGE")
        if not message:
            continue
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
        "summary":        get("SUMMARY"),
        "verdict":        verdict,
        "score":          score,
        "issues":         issues,
        "positives":      positives,
        "github_comment": get("COMMENT"),
    }


def build_github_body(review: dict) -> str:
    counts = {"critical": 0, "warning": 0, "style": 0, "info": 0}
    for issue in review["issues"]:
        counts[issue.get("severity", "info")] = counts.get(issue.get("severity", "info"), 0) + 1

    table = (
        "| Severity | Count |\n|----------|-------|\n"
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
        positives_md = "\n## Positives\n" + "\n".join(f"- {p}" for p in review["positives"])

    return (
        f"## Claude PR Review — Score: {review['score']}/100\n\n"
        f"{table}\n\n"
        f"**Summary:** {review['summary']}\n\n"
        f"## Issues\n{issues_md}{positives_md}"
    )


def post_github_review(review: dict) -> None:
    token  = os.environ["GITHUB_TOKEN"]
    repo   = os.environ["GITHUB_REPOSITORY"]
    pr_num = os.environ["PR_NUMBER"]
    event  = {"approve": "APPROVE", "request_changes": "REQUEST_CHANGES"}.get(
        review["verdict"], "COMMENT"
    )
    resp = requests.post(
        f"https://api.github.com/repos/{repo}/pulls/{pr_num}/reviews",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"body": build_github_body(review), "event": event},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"✅  Review posted — verdict: {event}, score: {review['score']}/100")


def print_local_report(review: dict) -> None:
    divider = "─" * 60
    print(f"\n{divider}")
    print(f"  Score: {review['score']}/100  |  Verdict: {review['verdict'].upper()}")
    print(f"  {review['summary']}")
    print(divider)
    for i, iss in enumerate(review["issues"], 1):
        line_info = f" [line {iss['line']}]" if iss.get("line") else ""
        print(f"\n  {i}. [{iss['severity'].upper()}] {iss['category']}{line_info}")
        print(f"     {iss['message']}")
        if iss.get("suggestion"):
            for ln in textwrap.wrap(iss["suggestion"], 70):
                print(f"     → {ln}")
    if review["positives"]:
        print("\nPOSITIVES")
        for p in review["positives"]:
            print(f"  ✓ {p}")


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
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated ...]"

    print(f"🔍  Diff length: {len(diff)} chars")
    print("🤖  Calling Claude API…")

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": (
            f'PR: "{os.environ.get("PR_TITLE","")}" '
            f'({os.environ.get("GITHUB_REPOSITORY","")})\n\nDiff:\n{diff}'
        )}],
    )

    raw = message.content[0].text
    print("Raw response:\n", raw)

    # Verify the new prompt is working — raw must NOT start with ```json
    if raw.strip().startswith("```"):
        print("ERROR: Claude returned JSON/markdown instead of plain text.", file=sys.stderr)
        print("This means the old review.py is still in the repo. Re-check the file.", file=sys.stderr)
        sys.exit(1)

    review = parse_response(raw)
    print(f"✅  Parsed: {len(review['issues'])} issues, score {review['score']}, verdict {review['verdict']}")

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
        critical_count = sum(1 for i in review["issues"] if i.get("severity") == "critical")
        if critical_count > 0:
            print(f"\n❌  {critical_count} critical issue(s) found — failing the check.")
            sys.exit(1)

    print("✅  Review complete.")


if __name__ == "__main__":
    main()
