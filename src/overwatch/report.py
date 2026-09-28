"""Markdown rendering for the full report, issue bodies and PR summaries."""
from __future__ import annotations

import difflib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Config
from .models import SEVERITIES, Finding

SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
SOURCE_LABEL = {
    "gemini": "Gemini code review, verified by {verifier}",
    "docs": "Gemini documentation check, verified by {verifier}",
    "semgrep": "Semgrep rule, verified by {verifier}",
    "gitleaks": "Gitleaks secret scan",
    "osv": "OSV-Scanner dependency audit",
    "links": "Documentation link checker",
}


@dataclass
class RunSummary:
    files_analyzed: int = 0
    docs_analyzed: int = 0
    skipped: Counter = field(default_factory=Counter)
    tool_status: dict = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    verification: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ai_enabled: bool = True


def sev_label(severity: str) -> str:
    return f"{SEV_ICON.get(severity, '')} {severity.capitalize()}".strip()


def source_label(cfg: Config, f: Finding) -> str:
    label = SOURCE_LABEL.get(f.source, f.source).replace("{verifier}", cfg.verifier_label)
    return label if f.verified else label.split(",")[0] + " (not verified)"


def location_md(cfg: Config, f: Finding) -> str:
    if f.start_line > 0:
        span = f"line {f.start_line}" if f.end_line <= f.start_line else f"lines {f.start_line}–{f.end_line}"
        return f"[`{f.path}` {span}]({cfg.permalink(f.path, f.start_line, f.end_line)})"
    return f"[`{f.path}`]({cfg.permalink(f.path)})"


def edits_diff(edits: list[dict]) -> str:
    chunks = []
    for e in edits:
        diff = difflib.unified_diff(
            e["old"].splitlines(), e["new"].splitlines(),
            fromfile=f"a/{e['path']}", tofile=f"b/{e['path']}", lineterm="", n=2,
        )
        chunks.append("\n".join(diff))
    return "\n".join(chunks)


def finding_details(cfg: Config, f: Finding, level: int = 4) -> str:
    h = "#" * level
    parts = [
        f"**Severity:** {sev_label(f.severity)} · **Category:** `{f.category}` · **Source:** {source_label(cfg, f)}",
        f"**Location:** {location_md(cfg, f)}",
    ]
    if f.also_at:
        parts.append("**Also found at:** " + ", ".join(f"`{loc}`" for loc in f.also_at[:10]))
    parts += ["", f"{h} What's wrong", f.explanation or f.description or "_No details provided._"]
    if f.why:
        parts += ["", f"{h} Why it matters", f.why]
    if f.suggested_change:
        parts += ["", f"{h} Suggested change", f.suggested_change]
    if f.related_updates:
        parts += ["", f"{h} Linked files that also need updating"]
        parts += [f"- [`{u['path']}`]({cfg.permalink(u['path'])}) — {u['reason'] or 'affected by this change'}" for u in f.related_updates]
    elif f.related_files:
        parts += ["", f"{h} Linked files to check"]
        parts += [f"- [`{p}`]({cfg.permalink(p)})" for p in f.related_files]
    if f.edits:
        status = " (applied in the fix pull request)" if f.fix_applied else ""
        parts += [
            "", f"{h} Proposed patch{status}", "<details><summary>Show diff</summary>", "",
            "```diff", edits_diff(f.edits), "```", "", "</details>",
        ]
    if f.references:
        parts += ["", f"{h} References"] + [f"- {r}" for r in f.references]
    return "\n".join(parts)


def _counts_line(findings: list[Finding]) -> str:
    counts = Counter(f.severity for f in findings)
    return " · ".join(f"{sev_label(s)}: **{counts.get(s, 0)}**" for s in reversed(SEVERITIES))


def build_report(cfg: Config, findings: list[Finding], summary: RunSummary, issue_result=None, fix_result=None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit_link = f"[`{cfg.short_sha}`]({cfg.server_url}/{cfg.repo}/commit/{cfg.sha})" if cfg.sha else "`unknown`"
    lines = [
        "# Repo Overwatch report",
        "",
        f"**Repository:** `{cfg.repo}` · **Branch:** `{cfg.branch}` · **Commit:** {commit_link} · "
        f"**Trigger:** `{cfg.event_name}` · **Generated:** {now}"
        + (f" · [Workflow run]({cfg.run_url})" if cfg.run_url else ""),
        "",
        "## Summary",
        "",
        f"**{len(findings)} findings** — {_counts_line(findings)}",
        "",
    ]
    if not summary.ai_enabled:
        lines += ["> ⚠️ AI analysis was skipped because the Gemini API key was not available (for example on a pull request from a fork). Only the deterministic scanners ran.", ""]
    if fix_result is not None and fix_result.message:
        link = f" — [pull request]({fix_result.pr_url})" if fix_result.pr_url else ""
        lines += [f"**Fix pull request:** {fix_result.message}{link}", ""]
    if issue_result is not None:
        lines += [f"**Issues:** {issue_result.describe()}", ""]

    by_cat = Counter(f.category for f in findings)
    if by_cat:
        lines += ["| Category | Count |", "|---|---|"] + [f"| `{c}` | {n} |" for c, n in by_cat.most_common()] + [""]

    if findings:
        lines += ["## Findings", "", "| # | Severity | Category | Location | Finding | Issue |", "|---|---|---|---|---|---|"]
        for i, f in enumerate(findings, 1):
            issue = f"[#{f.issue_number}]({f.issue_url})" if f.issue_number else "—"
            title = f.title.replace("|", "\\|")
            lines.append(f"| {i} | {sev_label(f.severity)} | `{f.category}` | {location_md(cfg, f)} | {title} | {issue} |")
        lines += ["", "## Details", ""]
        for i, f in enumerate(findings, 1):
            lines += [f"### {i}. {f.title}", "", finding_details(cfg, f), "", "---", ""]
    else:
        lines += ["No problems found. 🎉", ""]

    lines += ["## Run details", ""]
    lines.append(f"- Files analyzed: **{summary.files_analyzed}** code/config, **{summary.docs_analyzed}** documentation")
    if summary.skipped:
        lines.append("- Files skipped: " + ", ".join(f"{n} {reason}" for reason, n in summary.skipped.most_common()))
    lines.append(f"- Analysis cache: {summary.cache_hits} reused, {summary.cache_misses} new")
    for tool, status in summary.tool_status.items():
        lines.append(f"- {tool}: {status}")
    if summary.verification:
        v = summary.verification
        pending = f", {v['pending']} still waiting" if v.get("pending") else ""
        lines.append(f"- Verification ({cfg.verifier_label}): {v.get('candidates', 0)} candidates → {v.get('confirmed', 0)} confirmed, {v.get('rejected', 0)} rejected as false positives{pending}")
    for model, u in summary.usage.items():
        lines.append(f"- {model}: {u['calls']} calls, {u['input_tokens']:,} input / {u['output_tokens']:,} output tokens")
    if fix_result is not None and fix_result.failed:
        lines += ["", "### Fixes that could not be applied automatically", ""]
        lines += [f"- {f.title} (`{f.path}`): {reason}" for f, reason in fix_result.failed]
    if fix_result is not None and fix_result.validation_log:
        lines += ["", "### Validation output", "", "```", fix_result.validation_log[-6000:], "```"]
    if issue_result is not None and issue_result.overflow:
        lines += ["", f"### Findings not filed as issues (limit of {cfg.max_new_issues} new issues per run)", ""]
        lines += [f"- {sev_label(f.severity)} {f.title} — {location_md(cfg, f)}" for f in issue_result.overflow]
    if summary.notes:
        lines += ["", "### Notes", ""] + [f"- {n}" for n in summary.notes]
    if summary.errors:
        lines += ["", "### Errors", "", "Some analysis steps did not finish, so this report may be incomplete. Stale issues were not auto-closed on this run.", ""]
        lines += [f"- {e}" for e in summary.errors]
    return "\n".join(lines) + "\n"


def short_summary(cfg: Config, findings: list[Finding], fix_result=None, limit: int = 15) -> str:
    """Compact version used for pull request comments."""
    lines = [
        "### Repo Overwatch",
        "",
        f"Scanned `{cfg.branch}` at `{cfg.short_sha}`: **{len(findings)} findings** — {_counts_line(findings)}",
        "",
    ]
    for f in findings[:limit]:
        issue = f" ([#{f.issue_number}]({f.issue_url}))" if f.issue_number else ""
        lines.append(f"- {sev_label(f.severity)} **{f.title}** — {location_md(cfg, f)}{issue}")
    if len(findings) > limit:
        lines.append(f"- ...and {len(findings) - limit} more")
    if fix_result is not None and fix_result.pr_url:
        lines += ["", f"Suggested fixes: {fix_result.pr_url}"]
    if cfg.run_url:
        lines += ["", f"Full report: {cfg.run_url} (job summary and `overwatch-report` artifact)"]
    return "\n".join(lines)


def truncate(text: str, limit: int, note: str) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(note) - 10] + "\n\n" + note + "\n"
