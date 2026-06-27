# .github/scripts/labels.py
import requests
import os


def ensure_label_exists(token: str, repo: str, name: str, color: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/labels/{requests.utils.quote(name)}",
        headers=headers, timeout=10
    )
    if resp.status_code == 404:
        requests.post(
            f"https://api.github.com/repos/{repo}/labels",
            headers=headers,
            json={"name": name, "color": color},
            timeout=10,
        )


def apply_label(token: str, repo: str, pr_number: int, label: str) -> None:
    requests.post(
        f"https://api.github.com/repos/{repo}/issues/{pr_number}/labels",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"labels": [label]},
        timeout=10,
    )


def remove_label(token: str, repo: str, pr_number: int, label: str) -> None:
    requests.delete(
        f"https://api.github.com/repos/{repo}/issues/{pr_number}/labels/{requests.utils.quote(label)}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )


def set_review_label(token: str, repo: str, pr_number: int,
                     verdict: str, score: int, cfg: dict) -> None:
    if not cfg.get("labels", {}).get("enabled", True):
        return

    labels_cfg = cfg.get("labels", {})
    critical_label  = labels_cfg.get("critical_label",  "review: critical")
    approved_label  = labels_cfg.get("approved_label",  "review: approved")
    needs_work_label= labels_cfg.get("needs_work_label","review: needs-work")

    ensure_label_exists(token, repo, critical_label,   "d93f0b")
    ensure_label_exists(token, repo, approved_label,   "0e8a16")
    ensure_label_exists(token, repo, needs_work_label, "e4e669")

    # Remove all three first to avoid stale labels from a previous run
    for lbl in [critical_label, approved_label, needs_work_label]:
        remove_label(token, repo, pr_number, lbl)

    if verdict == "approve":
        apply_label(token, repo, pr_number, approved_label)
    elif verdict == "request_changes":
        apply_label(token, repo, pr_number, needs_work_label)
        if score <= 30:
            apply_label(token, repo, pr_number, critical_label)
