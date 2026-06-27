# .github/scripts/suppress.py
"""
Parses // prbot-ignore comments from diff lines.
Usage: if suppress.is_suppressed(line_number, diff_text): skip issue
"""

import re
from typing import Set


_SUPPRESS_RE = re.compile(r'//\s*prbot-ignore', re.IGNORECASE)


def suppressed_lines(diff_text: str) -> Set[int]:
    """
    Returns a set of line numbers (in the new file) that carry a prbot-ignore comment.
    Only looks at added lines (lines starting with +).
    """
    suppressed: Set[int] = set()
    current_new_line = 0

    for raw_line in diff_text.splitlines():
        # Hunk header: @@ -old_start,old_count +new_start,new_count @@
        hunk = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)', raw_line)
        if hunk:
            current_new_line = int(hunk.group(1)) - 1
            continue
        if raw_line.startswith('+'):
            current_new_line += 1
            content = raw_line[1:]  # strip the leading +
            if _SUPPRESS_RE.search(content):
                suppressed.add(current_new_line)
        elif not raw_line.startswith('-'):
            current_new_line += 1

    return suppressed


def filter_issues(issues: list, diff_text: str) -> list:
    """Remove issues whose line is suppressed in the diff."""
    suppressed = suppressed_lines(diff_text)
    return [i for i in issues if i.get("line") not in suppressed]
