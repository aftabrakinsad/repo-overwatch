"""Applies verified patches on a branch, validates them and opens one fix PR.

Safety rules:
- A patch is applied only if every "old" text matches exactly once.
- A finding's edits are applied all-or-nothing.
- Workflow files (.github/workflows) are never modified.
- If validation commands are configured and fail, no PR is opened.
- The workspace is always restored to the scanned commit afterwards.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .github_client import GitHub, GitHubError
from .log import log
from .models import Finding
from .report import sev_label

PROTECTED_PREFIXES = (".github/workflows/",)
FIX_BRANCH_PREFIX = "overwatch/fixes-"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
_LINE_PREFIX = re.compile(r"^\s*\d+\| ?", re.M)


@dataclass
class FixResult:
    status: str = "skipped"
    message: str = ""
    pr_url: str = ""
    pr_number: int | None = None
    applied: list[Finding] = field(default_factory=list)
    failed: list[tuple[Finding, str]] = field(default_factory=list)
    validation_log: str = ""
    patch_path: str = ""


def _git(ws: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def _strip_line_numbers(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if lines and all(_LINE_PREFIX.match(l) for l in lines if l.strip()):
        return _LINE_PREFIX.sub("", text)
    return text


def apply_edits(ws: Path, findings: list[Finding], allowed_paths: set[str]) -> tuple[list[Finding], list[tuple[Finding, str]], dict[str, str]]:
    """Returns (applied findings, failed findings with reasons, new contents by path)."""
    contents: dict[str, str] = {}
    applied: list[Finding] = []
    failed: list[tuple[Finding, str]] = []

    def current(path: str, staged: dict[str, str]) -> str:
        if path in staged:
            return staged[path]
        if path in contents:
            return contents[path]
        return (ws / path).read_bytes().decode("utf-8")

    for f in sorted(findings, key=lambda x: -x.rank):
        if not f.edits:
            continue
        staged: dict[str, str] = {}
        reason = ""
        for e in f.edits:
            path = e["path"]
            if path not in allowed_paths:
                reason = f"`{path}` is not a file Overwatch analyzed"
                break
            if path.startswith(PROTECTED_PREFIXES):
                reason = f"`{path}` is a workflow file; Overwatch never edits workflows"
                break
            try:
                text = current(path, staged)
            except (OSError, UnicodeDecodeError):
                reason = f"could not read `{path}`"
                break
            old, new = _strip_line_numbers(e["old"]), _strip_line_numbers(e["new"])
            if old not in text and "\r\n" in text:
                old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            matches = text.count(old)
            if matches != 1:
                reason = f"the patch text for `{path}` matched {matches} places (it must match exactly one)"
                break
            staged[path] = text.replace(old, new, 1)
        if reason:
            failed.append((f, reason))
            continue
        contents.update(staged)
        f.fix_applied = True
        applied.append(f)

    for path, text in contents.items():
        (ws / path).write_bytes(text.encode("utf-8"))
    return applied, failed, contents


def _validate(cfg: Config) -> tuple[bool, str]:
    output = []
    for command in cfg.validate:
        log.info(f"Validating fixes: {command}")
        try:
            proc = subprocess.run(command, shell=True, cwd=cfg.workspace, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            return False, "\n".join(output + [f"$ {command}", "timed out after 30 minutes"])
        output += [f"$ {command}", (proc.stdout + proc.stderr)[-4000:]]
        if proc.returncode != 0:
            return False, "\n".join(output + [f"(exit code {proc.returncode})"])
    return True, "\n".join(output)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "es" if noun.endswith(("x", "s", "ch", "sh")) else "s")


def _branch_name(base: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-") or "default"
    return FIX_BRANCH_PREFIX + slug


def _pr_body(cfg: Config, result: FixResult, validated: str) -> str:
    closing = cfg.branch == cfg.default_branch
    rows = []
    for f in result.applied:
        files = sorted({e["path"] for e in f.edits})
        issue = f"#{f.issue_number}" if f.issue_number else "—"
        rows.append(f"| {sev_label(f.severity)} | {f.title.replace('|', '/')} | {', '.join(f'`{p}`' for p in files)} | {issue} |")
    lines = [
        "## Automated fixes from Repo Overwatch",
        "",
        f"Each change below was proposed by Gemini, verified by {cfg.verifier_label}, and applied only where the patch matched the code exactly. "
        "**Please review every change before merging.** Inline review comments explain each fix.",
        "",
        f"**Base:** `{cfg.branch}` · **Scanned commit:** `{cfg.short_sha}`" + (f" · [Workflow run]({cfg.run_url})" if cfg.run_url else ""),
        "",
        "| Severity | Fix | Files | Issue |",
        "|---|---|---|---|",
        *rows,
        "",
        "### Validation",
        validated,
    ]
    if result.failed:
        lines += ["", f"{len(result.failed)} other proposed fixes could not be applied cleanly; they are described in their issues."]
    refs = [f"{'Fixes' if closing else 'Refs'} #{f.issue_number}" for f in result.applied if f.issue_number]
    if refs:
        lines += ["", *refs]
    lines += ["", "<!-- overwatch:fix-pr -->"]
    return "\n".join(lines)


def _review_comments(cfg: Config, applied: list[Finding], contents: dict[str, str]) -> list[dict]:
    comments = []
    for f in applied:
        for e in f.edits:
            new = e["new"].lstrip("\n")
            text = contents.get(e["path"], "")
            idx = text.find(new) if new.strip() else -1
            if idx < 0:
                continue
            line = text.count("\n", 0, idx) + 1
            body = [f"**{sev_label(f.severity)} — {f.title}**", "", f.explanation]
            if f.why:
                body += ["", f"**Why:** {f.why}"]
            if f.related_updates:
                body += ["", "**Also affects:** " + ", ".join(f"`{u['path']}`" for u in f.related_updates)]
            if f.issue_number:
                body += ["", f"Tracked in #{f.issue_number}"]
            comments.append({"path": e["path"], "line": line, "side": "RIGHT", "body": "\n".join(body)})
            break  # one comment per finding keeps the review readable
    return comments


def create_fix_pr(cfg: Config, gh: GitHub | None, findings: list[Finding], analyzed_paths: set[str]) -> FixResult:
    result = FixResult()
    if not cfg.create_fix_pr:
        result.message = "disabled by configuration"
        return result
    if cfg.is_fork_pr:
        result.message = "skipped for pull requests from forks"
        return result
    candidates = [f for f in findings if f.edits]
    if not candidates:
        result.status, result.message = "no-fixes", "no automatic fixes were proposed"
        return result

    ws = cfg.workspace
    if _git(ws, "status", "--porcelain", "--untracked-files=no", check=False):
        result.status, result.message = "error", "skipped because the working tree has uncommitted changes"
        return result
    original = _git(ws, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    original_sha = _git(ws, "rev-parse", "HEAD", check=False)
    restore_to = original if original and original != "HEAD" else original_sha
    branch = _branch_name(cfg.branch)

    try:
        _git(ws, "checkout", "-B", branch)
        result.applied, result.failed, contents = apply_edits(ws, candidates, analyzed_paths)
        if not result.applied:
            result.status, result.message = "no-fixes", "proposed fixes could not be applied cleanly"
            return result
        touched = sorted(contents)

        if cfg.validate:
            ok, output = _validate(cfg)
            result.validation_log = output
            if not ok:
                for f in result.applied:
                    f.fix_applied = False
                result.status = "validation-failed"
                result.message = "not opened: the fixes failed the validation commands (see the report)"
                return result
            validated = "✅ Passed: " + ", ".join(f"`{c}`" for c in cfg.validate)
        else:
            validated = "⚠️ No validation commands are configured, so these changes were not built or tested. Add a `validate:` list to `.overwatch.yml`."

        if cfg.dry_run or gh is None:
            patch = cfg.output_dir / "overwatch-fixes.patch"
            patch.parent.mkdir(parents=True, exist_ok=True)
            patch.write_text(_git(ws, "diff", "--", *touched) + "\n", encoding="utf-8")
            for f in result.applied:
                f.fix_applied = False  # nothing was actually committed
            result.status, result.patch_path = "dry-run", str(patch)
            result.message = f"dry run: {_plural(len(result.applied), 'fix')} written to {patch.name} instead of a pull request"
            return result

        _git(ws, "add", "--", *touched)
        _git(ws, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "--no-verify",
             "-m", f"fix: {_plural(len(result.applied), 'automated fix')} from Repo Overwatch",
             "-m", f"Scanned {cfg.branch} at {cfg.short_sha}.")

        pushed = True
        remote = _git(ws, "ls-remote", "--heads", "origin", branch, check=False)
        if remote:
            _git(ws, "fetch", "--depth=1", "origin", branch, check=False)
            if _git(ws, "rev-parse", "FETCH_HEAD^{tree}", check=False) == _git(ws, "rev-parse", "HEAD^{tree}"):
                pushed = False  # identical fixes are already on the branch
        if pushed:
            _git(ws, "push", "--force", "origin", f"HEAD:refs/heads/{branch}")

        body = _pr_body(cfg, result, validated)
        title = f"Repo Overwatch: {_plural(len(result.applied), 'automated fix')} for {cfg.branch}"
        existing = gh.find_open_pr(branch, cfg.branch)
        if existing:
            pr = gh.update_pr(existing["number"], title=title, body=body)
            result.status = "updated" if pushed else "unchanged"
        else:
            pr = gh.create_pr(title, body, branch, cfg.branch, draft=cfg.fix_pr_draft)
            result.status = "created"
        result.pr_url, result.pr_number = pr["html_url"], pr["number"]
        result.message = {"created": "opened", "updated": "updated with new fixes", "unchanged": "already up to date"}[result.status]

        if pushed:
            comments = _review_comments(cfg, result.applied, contents)
            if comments:
                try:
                    gh.create_review(pr["number"], "Repo Overwatch: explanation of each automated fix.", comments)
                except GitHubError as exc:
                    log.warning(f"Inline review comments failed ({exc}); posting a single comment instead.")
                    summary = "\n\n".join(f"`{c['path']}` line {c['line']}:\n\n{c['body']}" for c in comments)
                    gh.comment(pr["number"], summary[:60_000])
        return result
    except GitHubError as exc:
        result.status = "error"
        if exc.status == 403 and "not permitted to create" in exc.message:
            result.message = ("GitHub Actions is not allowed to open pull requests in this repository. Enable "
                              "Settings → Actions → General → 'Allow GitHub Actions to create and approve pull requests'.")
        else:
            result.message = f"GitHub API error: {exc}"
        log.warning(result.message)
        return result
    except Exception as exc:
        result.status, result.message = "error", f"failed: {exc}"
        log.warning(f"Fix pull request {result.message}")
        return result
    finally:
        _git(ws, "checkout", "--force", restore_to, check=False)
