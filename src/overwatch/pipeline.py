"""Orchestrates one Overwatch run from file discovery to report."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import analyzer, depgraph, fixer, issues, repo, report, scanners, verifier
from .cache import Cache
from .config import Config
from .github_client import GitHub
from .log import log
from .models import SEVERITY_RANK, FileInfo, Finding, compute_fingerprint


def _file_text(cfg: Config, files: dict[str, FileInfo], path: str) -> str | None:
    if path in files:
        return files[path].text
    try:  # e.g. a secret found in a file that is excluded from AI analysis; hashed locally only
        return (cfg.workspace / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def dedupe(cfg: Config, findings: list[Finding], files: dict[str, FileInfo]) -> list[Finding]:
    by_fp: dict[str, Finding] = {}
    for f in findings:
        f.fingerprint = f.fingerprint or compute_fingerprint(f, _file_text(cfg, files, f.path))
        keep = by_fp.get(f.fingerprint)
        if keep is None:
            by_fp[f.fingerprint] = f
            continue
        if f.rank > keep.rank:
            by_fp[f.fingerprint], keep, f = f, f, keep
        if f.start_line and f.start_line != keep.start_line:
            keep.also_at.append(f"{f.path}:{f.start_line}")
        if not keep.edits and f.edits:
            keep.edits = f.edits
        known = {u["path"] for u in keep.related_updates}
        keep.related_updates += [u for u in f.related_updates if u["path"] not in known]
    return sorted(by_fp.values(), key=lambda x: (-x.rank, x.path, x.start_line))


def _write_outputs(values: dict[str, str]) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write(f"{key}={value}\n")


def _step_summary(markdown: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        text = report.truncate(markdown, 900_000, "_(truncated; see the overwatch-report artifact)_")
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(text)


def main(cfg: Config, gemini=None, claude=None, gh: GitHub | None = None) -> int:
    if cfg.skip_reason:
        log.info(f"Repo Overwatch: nothing to do because {cfg.skip_reason}.")
        _write_outputs({"findings-count": "0", "report-path": "", "fix-pr-url": ""})
        return 0

    log.info(f"Repo Overwatch scanning {cfg.repo} @ {cfg.branch} ({cfg.short_sha}), event: {cfg.event_name}"
             + (" [dry run]" if cfg.dry_run else ""))
    summary = report.RunSummary()
    summary.ai_enabled = bool(cfg.gemini_api_key or gemini)
    cache = Cache(cfg.cache_dir)

    with log.group("Collecting files"):
        files, all_paths, skipped = repo.collect(cfg)
        summary.files_analyzed = sum(not f.is_doc for f in files.values())
        summary.docs_analyzed = sum(f.is_doc for f in files.values())
        summary.skipped = skipped
        graph = depgraph.build(files, all_paths)
        edges = sum(len(v) for v in graph.imports.values())
        log.info(f"{summary.files_analyzed} code/config files, {summary.docs_analyzed} docs, {edges} in-repo links")

    with log.group("Running deterministic scanners"):
        semgrep_candidates, deterministic, summary.tool_status = scanners.run_all(cfg, files, all_paths)
        deterministic += depgraph.broken_links(files, all_paths)
        for tool, status in summary.tool_status.items():
            log.info(f"{tool}: {status}")

    verified: list[Finding] = []
    if summary.ai_enabled:
        gemini, checker, checker_name = _ai_clients(cfg, gemini, claude)
        with log.group("Gemini first pass"):
            code_findings, errors = analyzer.analyze_code(cfg, files, graph, semgrep_candidates, gemini, cache)
            summary.errors += errors
            doc_findings, errors = analyzer.analyze_docs(cfg, files, gemini, cache)
            summary.errors += errors
            log.info(f"Gemini proposed {len(code_findings)} code and {len(doc_findings)} documentation candidates")
        with log.group(f"Verification ({cfg.verifier_label})"):
            candidates = code_findings + doc_findings + semgrep_candidates
            verified, errors, summary.verification = verifier.verify(
                cfg, files, graph, candidates, checker, checker_name, cache, all_paths
            )
            summary.errors += errors
            log.info(f"Verification confirmed {len(verified)} of {len(candidates)} candidates")
        clients = {f"Gemini {cfg.gemini_model}": gemini}
        if checker is not gemini:
            clients[checker_name] = checker
        for name, client in clients.items():
            usage = getattr(client, "usage", None)
            if usage is not None:
                summary.usage[name] = {"calls": usage.calls, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        if any(e.startswith("Stopped early") for e in summary.errors):
            summary.notes.append(
                "**This scan stopped early to stay within your AI quota.** Everything finished so far is cached. "
                "Comment `run overwatch` again to continue from where it stopped. If the daily quota was used up, "
                "wait until it resets (midnight Pacific time for Gemini's free tier)."
            )
    else:
        log.warning("No Gemini API key is available; running deterministic scanners only.")
        for c in semgrep_candidates:  # unverified, so keep them clearly labelled
            c.explanation = c.description + "\n\n_Not verified: AI analysis was unavailable for this run._"
        verified = semgrep_candidates

    summary.cache_hits, summary.cache_misses = cache.hits, cache.misses
    all_findings = dedupe(cfg, verified + deterministic, files)
    present = {f.fingerprint for f in all_findings}
    findings = [f for f in all_findings if f.rank >= SEVERITY_RANK[cfg.min_severity]]
    scan_complete = summary.ai_enabled and not summary.errors
    log.info(f"{len(findings)} findings at or above '{cfg.min_severity}' ({len(all_findings)} total)")

    if gh is None and not cfg.dry_run and cfg.github_token and cfg.in_actions:
        gh = GitHub(cfg.github_token, cfg.repo, cfg.api_url)
    can_write = gh is not None and not cfg.dry_run and not cfg.is_fork_pr

    issue_result = None
    if can_write and cfg.create_issues:
        with log.group("Syncing GitHub issues"):
            issue_result, error = issues.safe("Issue sync", issues.sync, cfg, gh, findings, present, scan_complete)
            if issue_result is None:
                issue_result = issues.IssueResult(error=error)
            log.info(f"Issues: {issue_result.describe()}")

    fix_result = None
    if summary.ai_enabled:
        with log.group("Preparing fix pull request"):
            fix_result = fixer.create_fix_pr(cfg, gh if can_write else None, findings, set(files))
            log.info(f"Fix pull request: {fix_result.message or fix_result.status}")

    if not scan_complete and summary.ai_enabled:
        summary.notes.append("Stale issues were not auto-closed because part of the analysis failed.")
    markdown = report.build_report(cfg, findings, summary, issue_result, fix_result)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = cfg.output_dir / "overwatch-report.md"
    report_path.write_text(markdown, encoding="utf-8")
    (cfg.output_dir / "overwatch-findings.json").write_text(
        json.dumps([f.to_dict() for f in findings], indent=2), encoding="utf-8"
    )
    _step_summary(markdown)
    log.info(f"Report written to {report_path}")

    if can_write and cfg.create_issues and cfg.is_default_branch:
        url, _ = issues.safe("Updating the report issue", issues.upsert_report_issue, cfg, gh, markdown)
        if url:
            log.info(f"Report issue: {url}")
    if can_write and cfg.reply_to:
        # Triggered by a "run overwatch" comment: answer in the same thread.
        intro = f"@{cfg.requested_by} " if cfg.requested_by else ""
        issues.safe("Replying to the command", gh.comment, cfg.reply_to,
                    f"{intro}Scan finished.\n\n" + report.short_summary(cfg, findings, fix_result))
    elif can_write and cfg.pr_number:
        issues.safe("Pull request comment", issues.upsert_pr_comment, gh, cfg.pr_number,
                    report.short_summary(cfg, findings, fix_result))

    try:
        cache.save()
    except OSError as exc:
        log.warning(f"Could not save the analysis cache: {exc}")

    _write_outputs({
        "findings-count": str(len(findings)),
        "report-path": str(report_path),
        "fix-pr-url": fix_result.pr_url if fix_result else "",
    })

    if cfg.fail_on in SEVERITY_RANK:
        threshold = SEVERITY_RANK[cfg.fail_on]
        blocking = [f for f in findings if f.rank >= threshold]
        if blocking:
            log.error(f"{len(blocking)} findings at or above '{cfg.fail_on}' (fail-on setting).")
            return 1
    return 0


def _ai_clients(cfg: Config, gemini=None, claude=None):
    """Returns (first-pass client, verifier client, verifier model name).

    Claude verifies when an Anthropic key is present (or verifier: claude). Otherwise a
    second, stricter Gemini pass verifies. One Budget caps AI requests for the whole run;
    each Gemini model gets its own rate limiter because quotas are per model.
    """
    from .llm import Budget, ClaudeClient, GeminiClient, RateLimiter

    budget = Budget(cfg.max_ai_requests)
    if gemini is None:
        gemini = GeminiClient(cfg.gemini_api_key, cfg.gemini_model,
                              RateLimiter(cfg.gemini_rpm, cfg.gemini_tpm), budget)
    has_claude = bool(claude or cfg.anthropic_api_key)
    use_claude = cfg.verifier == "claude" or (cfg.verifier == "auto" and has_claude)
    if use_claude and not has_claude:
        log.warning("verifier is set to claude but no Anthropic API key is available; verifying with Gemini instead.")
        use_claude = False
    if use_claude:
        checker = claude or ClaudeClient(cfg.anthropic_api_key, cfg.claude_model, budget)
        cfg.verifier_label = "Claude"
        return gemini, checker, f"Claude {cfg.claude_model}"
    verify_model = cfg.gemini_verify_model or cfg.gemini_model
    if verify_model == cfg.gemini_model:
        checker = gemini  # same model: share its rate limiter
    else:
        checker = GeminiClient(cfg.gemini_api_key, verify_model,
                               RateLimiter(cfg.gemini_rpm, cfg.gemini_tpm), budget)
    cfg.verifier_label = "a second Gemini pass"
    return gemini, checker, f"Gemini {verify_model} (verifier)"


def run_from_path(workspace: Path, dry_run: bool | None = None, config_path: str | None = None) -> int:
    from .config import load

    return main(load(workspace, dry_run=dry_run, config_path=config_path))
