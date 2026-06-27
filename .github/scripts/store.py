# .github/scripts/store.py
"""
Persists PR review results to a SQLite database uploaded/downloaded
as a GitHub Actions artifact. Zero external infrastructure needed.
"""

import sqlite3
import os
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("REVIEW_DB_PATH", ".github/data/reviews.db"))


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            repo        TEXT NOT NULL,
            pr_number   INTEGER NOT NULL,
            pr_title    TEXT,
            author      TEXT,
            score       INTEGER,
            verdict     TEXT,
            issues_json TEXT,
            created_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_repo_created
        ON reviews(repo, created_at)
    """)
    conn.commit()
    return conn


def save_review(repo: str, pr_number: int, pr_title: str,
                author: str, score: int, verdict: str, issues: list) -> None:
    conn = _connect()
    conn.execute("""
        INSERT INTO reviews (repo, pr_number, pr_title, author, score, verdict, issues_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (repo, pr_number, pr_title, author, score, verdict,
          json.dumps(issues), datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()


def get_recent_reviews(repo: str, days: int = 30) -> list[dict]:
    conn = _connect()
    rows = conn.execute("""
        SELECT * FROM reviews
        WHERE repo = ?
          AND created_at >= datetime('now', ?)
        ORDER BY created_at DESC
    """, (repo, f"-{days} days")).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_author_stats(repo: str, days: int = 30) -> list[dict]:
    conn = _connect()
    rows = conn.execute("""
        SELECT
            author,
            COUNT(*)           AS total_prs,
            ROUND(AVG(score))  AS avg_score,
            MIN(score)         AS min_score,
            MAX(score)         AS max_score,
            SUM(CASE WHEN verdict = 'request_changes' THEN 1 ELSE 0 END) AS blocked_prs
        FROM reviews
        WHERE repo = ?
          AND created_at >= datetime('now', ?)
        GROUP BY author
        ORDER BY avg_score ASC
    """, (repo, f"-{days} days")).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recurring_issues(repo: str, days: int = 30, min_count: int = 2) -> list[dict]:
    """Find issue categories that keep appearing across PRs."""
    conn = _connect()
    rows = conn.execute("""
        SELECT author, issues_json FROM reviews
        WHERE repo = ?
          AND created_at >= datetime('now', ?)
    """, (repo, f"-{days} days")).fetchall()
    conn.close()

    from collections import Counter
    category_counts: dict[str, Counter] = {}
    for row in rows:
        author = row["author"]
        issues = json.loads(row["issues_json"] or "[]")
        if author not in category_counts:
            category_counts[author] = Counter()
        for issue in issues:
            category_counts[author][issue.get("category", "Unknown")] += 1

    recurring = []
    for author, counter in category_counts.items():
        for category, count in counter.items():
            if count >= min_count:
                recurring.append({"author": author, "category": category, "count": count})

    return sorted(recurring, key=lambda x: x["count"], reverse=True)


def save_feedback(repo: str, pr_number: int, issue_index: int, is_positive: bool) -> None:
    """Record a thumbs-up/down reaction on an issue."""
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            repo        TEXT,
            pr_number   INTEGER,
            issue_index INTEGER,
            is_positive INTEGER,
            created_at  TEXT
        )
    """)
    conn.execute("""
        INSERT INTO feedback (repo, pr_number, issue_index, is_positive, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (repo, pr_number, issue_index, int(is_positive),
          datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
