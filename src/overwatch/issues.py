"""Keeps one GitHub issue per finding in sync with the latest scan.

- New finding: open an issue (up to max_new_issues per run).
- Known finding, issue open: refresh the body if the analysis changed.
- Known finding, issue closed as "completed": reopen it (the problem is back).
- Known finding, issue closed as "not planned" or labelled overwatch-ignore: stay quiet.
- On the default branch only, after a complete scan: close issues whose finding is gone.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

from .config import Config
from .github_client import GitHub, GitHubError
from .log import log
from .models import Finding
from .report import finding_details, truncate

LABEL = "overwatch"
IGNORE_LABEL = "overwatch-ignore"
REPORT_LABEL = "overwatch-report"
FP_RE = re.compile(r"<!-- overwatch:fingerprint=([0-9a-f]{16}) -->")
CONTENT_RE = re.compile(r"<!-- overwatch:content=([0-9a-f]{12}) -->")
PR_MARKER = "<!-- overwatch:pr-summary -->"
MAX_BODY = 60_000

LABELS = {
    LABEL: ("6f42c1", "Found by Repo Overwatch"),
    IGNORE_LABEL: ("cfd3d7", "Overwatch will not reopen or update this issue"),
    REPORT_LABEL: ("6f42c1", "Latest Repo Overwatch report"),
    "severity:critical": ("b60205", "Overwatch severity"),
    "severity:high": ("d93f0b", "Overwatch severity"),
    "severity:medium": ("fbca04", "Overwatch severity"),
    "severity:low": ("0e8a16", "Overwatch severity"),
}


@dataclass
class IssueResult:
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    reopened: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    dismissed: int = 0
    overflow: list[Finding] = field(default_factory=list)
    error: str = ""

    def describe(self) -> str:
        if self.error:
            return f"could not be synced ({self.error})"
        parts = [
            f"{len(self.created)} opened",
            f"{len(self.updated)} updated",
            f"{len(self.reopened)} reopened",
            f"{len(self.closed)} closed as fixed",
        ]
        if self.dismissed:
            parts.append(f"{self.dismissed} skipped (dismissed by you)")
        if self.overflow:
            parts.append(f"{len(self.overflow)} over the per-run limit")
        return ", ".join(parts)


def issue_title(f: Finding) -> str:
    return f"[Overwatch][{f.severity.upper()}] {f.title}"[:240]


def issue_body(cfg: Config, f: Finding) -> str:
    core = finding_details(cfg, f, level=3)
    content_hash = hashlib.sha1(core.encode("utf-8")).hexdigest()[:12]
    footer = (
        f"\n\n---\n<sub>Detected by Repo Overwatch on `{cfg.branch}` at commit `{cfg.short_sha}`"
        + (f" · [workflow run]({cfg.run_url})" if cfg.run_url else "")
        + ". Close as <b>not planned</b> or add the <code>overwatch-ignore</code> label to silence this finding.</sub>\n"
        f"<!-- overwatch:fingerprint={f.fingerprint} -->\n<!-- overwatch:content={content_hash} -->"
    )
    return truncate(core, MAX_BODY, "_(truncated; see the full report in the workflow run)_") + footer


def _labels_of(issue: dict) -> set[str]:
    return {l["name"] if isinstance(l, dict) else str(l) for l in issue.get("labels") or []}


def sync(cfg: Config, gh: GitHub, findings: list[Finding], present: set[str], scan_complete: bool) -> IssueResult:
    result = IssueResult()
    gh.ensure_labels(LABELS)

    existing: dict[str, dict] = {}
    for issue in gh.list_issues(LABEL):
        match = FP_RE.search(issue.get("body") or "")
        if not match:
            continue
        fp = match.group(1)
        if fp not in existing or issue["state"] == "open":
            existing[fp] = issue

    new_count = 0
    for f in sorted(findings, key=lambda x: (-x.rank, x.path, x.start_line)):
        body = issue_body(cfg, f)
        labels = [LABEL, f"severity:{f.severity}"]
        issue = existing.get(f.fingerprint)
        if issue is None:
            if new_count >= cfg.max_new_issues:
                result.overflow.append(f)
                continue
            created = gh.create_issue(issue_title(f), body, labels)
            new_count += 1
            result.created.append(created["number"])
            f.issue_number, f.issue_url = created["number"], created["html_url"]
            time.sleep(1)  # stay well under GitHub's secondary rate limits for content creation
            continue

        f.issue_number, f.issue_url = issue["number"], issue["html_url"]
        issue_labels = _labels_of(issue)
        if IGNORE_LABEL in issue_labels or (issue["state"] == "closed" and issue.get("state_reason") == "not_planned"):
            result.dismissed += 1
            continue
        if issue["state"] == "closed":
            gh.update_issue(issue["number"], state="open", title=issue_title(f), body=body,
                            labels=sorted((issue_labels - {l for l in issue_labels if l.startswith("severity:")}) | set(labels)))
            gh.comment(issue["number"], f"Repo Overwatch detected this problem again on `{cfg.branch}` at commit `{cfg.short_sha}`, so the issue was reopened.")
            result.reopened.append(issue["number"])
            continue
        old = CONTENT_RE.search(issue.get("body") or "")
        new = CONTENT_RE.search(body)
        if not old or not new or old.group(1) != new.group(1) or f"severity:{f.severity}" not in issue_labels:
            gh.update_issue(issue["number"], title=issue_title(f), body=body,
                            labels=sorted((issue_labels - {l for l in issue_labels if l.startswith("severity:")}) | set(labels)))
            result.updated.append(issue["number"])

    if cfg.is_default_branch and scan_complete:
        for fp, issue in existing.items():
            if issue["state"] != "open" or fp in present or IGNORE_LABEL in _labels_of(issue):
                continue
            gh.comment(issue["number"], f"Repo Overwatch no longer detects this problem on `{cfg.branch}` (commit `{cfg.short_sha}`): the code was fixed or removed, or the file is now excluded from scans. Closing.")
            gh.update_issue(issue["number"], state="closed", state_reason="completed")
            result.closed.append(issue["number"])
    elif not cfg.is_default_branch:
        log.info(f"Not auto-closing issues: `{cfg.branch}` is not the default branch `{cfg.default_branch}`")
    return result


def upsert_report_issue(cfg: Config, gh: GitHub, report_md: str) -> str:
    body = truncate(report_md, MAX_BODY, "_(report truncated; download the full report from the workflow run's artifacts)_")
    open_reports = gh.list_issues(REPORT_LABEL, state="open")
    if open_reports:
        issue = open_reports[0]
        gh.update_issue(issue["number"], body=body)
        return issue["html_url"]
    created = gh.create_issue(f"Repo Overwatch report: {cfg.repo}", body, [REPORT_LABEL])
    return created["html_url"]


def upsert_pr_comment(gh: GitHub, pr_number: int, summary_md: str) -> None:
    body = f"{summary_md}\n\n{PR_MARKER}"
    for comment in gh.list_comments(pr_number):
        if PR_MARKER in (comment.get("body") or ""):
            gh.update_comment(comment["id"], body)
            return
    gh.comment(pr_number, body)


def safe(action: str, fn, *args, **kwargs):
    """Run a GitHub side effect without letting an API failure crash the whole scan."""
    try:
        return fn(*args, **kwargs), ""
    except GitHubError as exc:
        hint = ""
        if exc.status in (401, 403):
            hint = " Check the workflow's `permissions:` block and Settings → Actions → General → Workflow permissions."
        log.warning(f"{action} failed: {exc}.{hint}")
        return None, f"{exc}{hint}"
