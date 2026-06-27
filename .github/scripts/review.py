# .github/scripts/review.py
"""
Claude PR Review Bot — full upgrade
"""

import os
import sys
import subprocess
import textwrap
import re
import requests
import anthropic
import yaml
from pathlib import Path

# NEW: import the helper modules
from store    import save_review, get_recent_reviews, get_recurring_issues
from suppress import filter_issues
from labels   import set_review_label

CLAUDE_MODEL   = "claude-sonnet-4-6"
MAX_TOKENS     = 4096
MAX_DIFF_CHARS = 15_000

# ─── NEW: load .prbot.yml config ──────────────────────────────────────────────

def load_config() -> dict:
    config_path = Path(".prbot.yml")
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


# ─── NEW: build language-aware system prompt ──────────────────────────────────

def build_system_prompt(cfg: dict) -> str:
    lang_rules = cfg.get("language_rules", {})
    checks     = cfg.get("checks", {})

    lang_section = ""
    if lang_rules.get("csharp") == "strict":
        lang_section += """
C# STRICT rules: flag all of — raw SqlCommand/SqlConnection (use parameterized queries or Dapper),
missing IDisposable.Dispose() or using statements, async void (use async Task),
ConfigureAwait missing on library code, DI constructor injection violations,
hardcoded connection strings or secrets, missing null checks on public API inputs.
"""
    if lang_rules.get("sql") == "strict":
        lang_section += """
SQL STRICT rules: flag SELECT * in stored procs, missing WHERE on UPDATE/DELETE,
implicit type conversions in WHERE clauses, missing indexes on FK columns,
non-sargable predicates (functions on indexed columns in WHERE).
"""

    extra_checks = ""
    if checks.get("test_coverage", True):
        extra_checks += "- If a public method is added or changed, check for a corresponding test. Flag if missing.\n"
    if checks.get("dependency_scan", True):
        extra_checks += "- If a new NuGet/pip/npm package is added, flag it as an info item so reviewers can vet it.\n"
    if checks.get("pr_description", True):
        extra_checks += "- If PR_DESCRIPTION is empty or under 20 characters, add an info issue flagging it.\n"
    if checks.get("commit_messages", True):
        extra_checks += "- If COMMIT_MESSAGES are not following conventional commits (feat/fix/chore/docs/refactor), add a style issue.\n"

    return f"""
You are a senior software engineer performing a code review.
Respond using EXACTLY this format. Nothing else. No JSON. No markdown. No extra text.

VERDICT: <approve|request_changes|comment>
SCORE: <integer 0-100>
SUMMARY: <one sentence>
ISSUE1_LINE: <integer or null>
ISSUE1_SEVERITY: <critical|warning|style|info>
ISSUE1_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL>
ISSUE1_MESSAGE: <one sentence, no code>
ISSUE1_SUGGESTION: <one sentence>
ISSUE1_FIXED_CODE: <corrected code snippet, use /// as line separator>
ISSUE2_LINE: <integer or null>
ISSUE2_SEVERITY: <critical|warning|style|info>
ISSUE2_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL>
ISSUE2_MESSAGE: <one sentence, no code>
ISSUE2_SUGGESTION: <one sentence>
ISSUE2_FIXED_CODE: <corrected code snippet, use /// as line separator>
ISSUE3_LINE: <integer or null>
ISSUE3_SEVERITY: <critical|warning|style|info>
ISSUE3_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL>
ISSUE3_MESSAGE: <one sentence, no code>
ISSUE3_SUGGESTION: <one sentence>
ISSUE3_FIXED_CODE: <corrected code snippet, use /// as line separator>
ISSUE4_LINE: <integer or null>
ISSUE4_SEVERITY: <critical|warning|style|info>
ISSUE4_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL>
ISSUE4_MESSAGE: <one sentence, no code>
ISSUE4_SUGGESTION: <one sentence>
ISSUE4_FIXED_CODE: <corrected code snippet, use /// as line separator>
ISSUE5_LINE: <integer or null>
ISSUE5_SEVERITY: <critical|warning|style|info>
ISSUE5_CATEGORY: <Security|Bug|Performance|Async|Dispose|DI|ErrorHandling|Style|Architecture|TSQL>
ISSUE5_MESSAGE: <one sentence, no code>
ISSUE5_SUGGESTION: <one sentence>
ISSUE5_FIXED_CODE: <corrected code snippet, use /// as line separator>
POSITIVES: <comma separated or none>
COMMENT: <one sentence>

Rules: max 5 issues, no newlines in any value except ISSUE_FIXED_CODE (use /// as line separator),
no backticks anywhere.

{lang_section}
{extra_checks}
"""


# ─── NEW: get PR description and commit messages for extra context ─────────────

def get_pr_metadata() -> dict:
    token  = os.environ.get("GITHUB_TOKEN", "")
    repo   = os.environ.get("GITHUB_REPOSITORY", "")
    pr_num = os.environ.get("PR_NUMBER", "")
    if not (token and repo and pr_num):
        return {"description": "", "commits": []}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    pr_resp = requests.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_num}",
        headers=headers, timeout=15
    )
    description = ""
    if pr_resp.ok:
        description = pr_resp.json().get("body") or ""

    commits_resp = requests.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_num}/commits",
        headers=headers, timeout=15
    )
    commits = []
    if commits_resp.ok:
        commits = [c["commit"]["message"].splitlines()[0] for c in commits_resp.json()]

    return {"description": description, "commits": commits}


# ─── diff helpers ──────────────────────────────────────────────────────────────

def get_diff() -> str:
    base = os.environ.get("BASE_SHA", "origin/main")
    head = os.environ.get("HEAD_SHA", "HEAD")
    result = subprocess.run(
        ["git", "diff", f"{base}...{head}"],
        capture_output=True, text=True, check=True
    )
    return result.stdout


# NEW: filter out ignored paths from diff
def filter_diff(diff: str, ignored_paths: list[str]) -> str:
    if not ignored_paths:
        return diff

    import fnmatch
    output_chunks = []
    current_file  = None
    current_chunk: list[str] = []

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git"):
            if current_file and current_chunk:
                output_chunks.append("".join(current_chunk))
            current_chunk = [line]
            # extract filename: diff --git a/foo.cs b/foo.cs
            m = re.search(r'diff --git a/(.+?) b/', line)
            current_file = m.group(1) if m else ""
            ignored = any(fnmatch.fnmatch(current_file, pat) for pat in ignored_paths)
            current_chunk = [] if ignored else [line]
        elif current_file and current_chunk is not None:
            current_chunk.append(line)

    if current_file and current_chunk:
        output_chunks.append("".join(current_chunk))

    return "".join(output_chunks)


# ─── parse response (unchanged from your original) ────────────────────────────

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

        fixed_code_raw = get(f"ISSUE{n}_FIXED_CODE")
        fixed_code = fixed_code_raw.replace("///", "\n") if fixed_code_raw else ""

        issues.append({
            "line":       line,
            "severity":   get(f"ISSUE{n}_SEVERITY") or "info",
            "category":   get(f"ISSUE{n}_CATEGORY") or "Style",
            "message":    message,
            "suggestion": get(f"ISSUE{n}_SUGGESTION"),
            "fixed_code": fixed_code,
        })

    return {
        "summary":        get("SUMMARY"),
        "verdict":        verdict,
        "score":          score,
        "issues":         issues,
        "positives":      positives,
        "github_comment": get("COMMENT"),
    }


# ─── GitHub comment builder ────────────────────────────────────────────────────

def detect_language(category: str, fixed_code: str) -> str:
    if category == "TSQL":
        return "sql"
    csharp_patterns = ["using ", "async ", "await ", "var ", "public ", "private ",
                       "protected ", "class ", "namespace ", "=> ", "SqlCommand"]
    for pattern in csharp_patterns:
        if pattern in fixed_code:
            return "csharp"
    return "csharp"


def build_github_body(review: dict, recurring: list) -> str:
    counts = {"critical": 0, "warning": 0, "style": 0, "info": 0}
    for issue in review["issues"]:
        sev = issue.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1

    table = (
        "| Severity | Count |\n|----------|-------|\n"
        + "\n".join(f"| {s.capitalize()} | {c} |" for s, c in counts.items())
    )

    issues_md = ""
    for i, iss in enumerate(review["issues"], 1):
        line_info = f" *(line {iss['line']})*" if iss.get("line") else ""
        fixed_code = iss.get("fixed_code", "").strip()
        lang = detect_language(iss.get("category", ""), fixed_code)

        # NEW: use GitHub suggestion syntax for one-click apply
        if fixed_code:
            code_block = f"\n\n**Fixed Code:**\n```suggestion\n{fixed_code}\n```"
        else:
            code_block = ""

        issues_md += (
            f"\n**{i}. [{iss['severity'].upper()}] {iss['category']}**{line_info}\n"
            f"{iss['message']}\n"
            f"> 💡 {iss['suggestion']}"
            f"{code_block}\n"
        )

    positives_md = ""
    if review["positives"]:
        positives_md = "\n## Positives\n" + "\n".join(f"- {p}" for p in review["positives"])

    # NEW: recurring issues section
    recurring_md = ""
    if recurring:
        lines = "\n".join(
            f"- **{r['author']}**: {r['category']} flagged {r['count']} times this month"
            for r in recurring[:5]
        )
        recurring_md = f"\n\n---\n## Recurring patterns this month\n{lines}"

    return (
        f"## Claude PR Review — Score: {review['score']}/100\n\n"
        f"{table}\n\n"
        f"**Summary:** {review['summary']}\n\n"
        f"## Issues\n{issues_md}{positives_md}{recurring_md}"
    )


# ─── NEW: post inline line comments ───────────────────────────────────────────

def post_inline_comments(review: dict, diff: str) -> None:
    """Post each issue as an inline comment pinned to its diff line."""
    token    = os.environ.get("GITHUB_TOKEN", "")
    repo     = os.environ.get("GITHUB_REPOSITORY", "")
    pr_num   = os.environ.get("PR_NUMBER", "")
    head_sha = os.environ.get("HEAD_SHA", "")
    if not (token and repo and pr_num and head_sha):
        return

    # Build a map of new-file line numbers → diff position
    # GitHub's API wants "position" (line number within the diff), not file line number
    file_line_to_position: dict[tuple[str, int], tuple[str, int]] = {}
    current_file = ""
    diff_position = 0

    for line in diff.splitlines():
        if line.startswith("diff --git"):
            m = re.search(r'diff --git a/(.+?) b/(.+)', line)
            if m:
                current_file = m.group(2)
            diff_position = 0
        elif line.startswith("@@"):
            # Reset position counter per hunk; count from 1 within hunk
            new_start = int(re.search(r'\+(\d+)', line).group(1))
            current_new_line = new_start - 1
            diff_position += 1
        elif line.startswith("+") and not line.startswith("+++"):
            current_new_line += 1
            diff_position += 1
            file_line_to_position[(current_file, current_new_line)] = (current_file, diff_position)
        elif not line.startswith("-"):
            current_new_line += 1
            diff_position += 1

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    for issue in review["issues"]:
        file_line = issue.get("line")
        if not file_line:
            continue  # no line number → skip inline, it appears in the summary comment

        # Find the right file from the diff
        matched = None
        for (fname, lineno), (_, pos) in file_line_to_position.items():
            if lineno == file_line:
                matched = (fname, pos)
                break

        if not matched:
            continue

        path, position = matched
        body = (
            f"**[{issue['severity'].upper()}] {issue['category']}**\n"
            f"{issue['message']}\n\n"
            f"💡 {issue['suggestion']}"
        )
        if issue.get("fixed_code"):
            body += f"\n\n```suggestion\n{issue['fixed_code']}\n```"

        requests.post(
            f"https://api.github.com/repos/{repo}/pulls/{pr_num}/comments",
            headers=headers,
            json={
                "body":       body,
                "commit_id":  head_sha,
                "path":       path,
                "position":   position,
            },
            timeout=15,
        )


# ─── post summary review ──────────────────────────────────────────────────────

def post_github_review(review: dict, recurring: list, cfg: dict) -> None:
    token  = os.environ["GITHUB_TOKEN"]
    repo   = os.environ["GITHUB_REPOSITORY"]
    pr_num = os.environ["PR_NUMBER"]

    score_cfg = cfg.get("score_thresholds", {})
    auto_approve = score_cfg.get("auto_approve", 85)
    auto_block   = score_cfg.get("auto_block",   40)

    if review["score"] >= auto_approve and not any(
        i["severity"] == "critical" for i in review["issues"]
    ):
        event = "APPROVE"
    elif review["score"] <= auto_block or review["verdict"] == "request_changes":
        event = "REQUEST_CHANGES"
    else:
        event = "COMMENT"

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/pulls/{pr_num}/reviews",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"body": build_github_body(review, recurring), "event": event},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Review posted — verdict: {event}, score: {review['score']}/100")


# ─── local report (unchanged) ─────────────────────────────────────────────────

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
        if iss.get("fixed_code"):
            print(f"     Fixed Code:\n     {'·'*50}")
            for ln in iss["fixed_code"].splitlines():
                print(f"       {ln}")
            print(f"     {'·'*50}")
    if review["positives"]:
        print("\nPOSITIVES")
        for p in review["positives"]:
            print(f"  + {p}")


# ─── main ──────────────────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    # NEW: load config
    cfg = load_config()
    print(f"Config loaded: {list(cfg.keys())}")

    print("Extracting diff…")
    try:
        diff = get_diff()
    except subprocess.CalledProcessError as exc:
        print(f"ERROR extracting diff: {exc}", file=sys.stderr)
        sys.exit(1)

    if not diff.strip():
        print("No diff found — nothing to review.")
        sys.exit(0)

    # NEW: filter ignored paths
    ignored = cfg.get("ignored_paths", [])
    diff = filter_diff(diff, ignored)
    if not diff.strip():
        print("All changed files are in ignored paths — skipping review.")
        sys.exit(0)

    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated ...]"

    # NEW: fetch PR metadata for extra context checks
    metadata = get_pr_metadata()

    print(f"Diff length: {len(diff)} chars")
    print("Calling Claude API…")

    client  = anthropic.Anthropic(api_key=api_key)
    prompt  = build_system_prompt(cfg)

    user_content = (
        f'PR: "{os.environ.get("PR_TITLE", "")} ({os.environ.get("GITHUB_REPOSITORY", "")})\n'
        f'PR_DESCRIPTION: {metadata["description"][:500] or "(empty)"}\n'
        f'COMMIT_MESSAGES: {"; ".join(metadata["commits"][:10]) or "(none)"}\n\n'
        f'Diff:\n{diff}'
    )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    raw = message.content[0].text
    if raw.strip().startswith("```"):
        print("ERROR: Claude returned JSON/markdown.", file=sys.stderr)
        sys.exit(1)

    review = parse_response(raw)
    print(f"Parsed: {len(review['issues'])} issues, score {review['score']}, verdict {review['verdict']}")

    # NEW: filter suppressed lines
    review["issues"] = filter_issues(review["issues"], diff)

    # NEW: look up recurring issues to surface in the comment
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    recurring = []
    if repo:
        try:
            recurring = get_recurring_issues(repo, days=30, min_count=2)
        except Exception:
            pass  # storage not initialised yet on first run — that's fine

    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("Posting review to GitHub…")
        try:
            post_github_review(review, recurring, cfg)
            post_inline_comments(review, diff)   # NEW: inline line comments
        except requests.HTTPError as exc:
            print(f"ERROR posting: {exc}\n{exc.response.text}", file=sys.stderr)
            sys.exit(1)

        # NEW: apply labels
        token = os.environ.get("GITHUB_TOKEN", "")
        pr_num = int(os.environ.get("PR_NUMBER", 0))
        if token and pr_num:
            set_review_label(token, repo, pr_num, review["verdict"], review["score"], cfg)

        # NEW: persist to SQLite
        author = os.environ.get("PR_AUTHOR", os.environ.get("GITHUB_ACTOR", "unknown"))
        try:
            save_review(
                repo=repo,
                pr_number=pr_num,
                pr_title=os.environ.get("PR_TITLE", ""),
                author=author,
                score=review["score"],
                verdict=review["verdict"],
                issues=review["issues"],
            )
            print("Review saved to database.")
        except Exception as e:
            print(f"Warning: could not save to database: {e}")

    else:
        print_local_report(review)

    # NEW: configurable severity threshold for CI failure
    threshold = cfg.get("severity_threshold", "warning")
    severity_order = ["info", "style", "warning", "critical"]
    if threshold != "none" and threshold in severity_order:
        threshold_idx = severity_order.index(threshold)
        blocking_issues = [
            i for i in review["issues"]
            if severity_order.index(i.get("severity", "info")) >= threshold_idx
        ]
        if blocking_issues:
            print(f"\n{len(blocking_issues)} issue(s) at or above '{threshold}' threshold — failing.")
            sys.exit(1)

    print("Review complete.")


if __name__ == "__main__":
    main()
