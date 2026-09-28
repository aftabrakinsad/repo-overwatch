"""Runs the free, deterministic scanners and converts their output into Findings.

- Semgrep results become *candidates*: the verifier checks them like Gemini's findings.
- Gitleaks and OSV-Scanner results are facts, reported directly. Secrets are
  redacted by Gitleaks and are never sent to any AI model.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import Config
from .models import FileInfo, Finding


def _run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _relpath(path: str, workspace: Path) -> str:
    if os.path.isabs(path):
        try:
            path = os.path.relpath(path, workspace)
        except ValueError:
            pass
    path = path.replace("\\", "/")
    return path[2:] if path.startswith("./") else path


def semgrep(cfg: Config, files: dict[str, FileInfo]) -> tuple[list[Finding], str]:
    if not shutil.which("semgrep"):
        return [], "skipped (not installed)"
    cmd = [
        "semgrep", "scan", "--config", cfg.semgrep_config, "--json", "--metrics=off",
        "--quiet", "--timeout", "30", "--max-target-bytes", str(cfg.max_file_bytes), ".",
    ]
    try:
        proc = _run(cmd, cfg.workspace, timeout=1200)
        data = json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        return [], f"error ({type(exc).__name__})"

    findings = []
    for r in data.get("results", []):
        path = _relpath(r.get("path", ""), cfg.workspace)
        if path not in files:  # respect Overwatch's include/exclude rules
            continue
        extra = r.get("extra") or {}
        meta = extra.get("metadata") or {}
        message = " ".join(str(extra.get("message", "")).split())
        category = "security" if (
            str(meta.get("category", "")).lower() == "security" or ".security." in r.get("check_id", "")
        ) else "bug"
        severity = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}.get(str(extra.get("severity", "")).upper(), "medium")
        start = int((r.get("start") or {}).get("line") or 1)
        end = int((r.get("end") or {}).get("line") or start)
        findings.append(
            Finding(
                source="semgrep",
                category=category,
                severity=severity,
                path=path,
                start_line=start,
                end_line=end,
                title=(message.split(". ")[0] or r.get("check_id", "Semgrep finding"))[:120],
                description=message,
                rule_id=r.get("check_id", ""),
                references=[str(u) for u in (meta.get("references") or [])][:3],
            )
        )
    errors = len(data.get("errors") or [])
    return findings, f"ran ({len(findings)} results{', ' + str(errors) + ' parse errors' if errors else ''})"


def gitleaks(cfg: Config, all_paths: list[str]) -> tuple[list[Finding], str]:
    if not shutil.which("gitleaks"):
        return [], "skipped (not installed)"
    tracked = set(all_paths)
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "gitleaks.json"
        cmd = [
            "gitleaks", "dir", ".", "--report-format", "json", "--report-path", str(report),
            "--redact", "--exit-code", "0", "--no-banner", "--log-level", "error",
        ]
        try:
            proc = _run(cmd, cfg.workspace, timeout=900)
            if not report.exists():
                return [], f"error (exit {proc.returncode}: {proc.stderr.strip()[:200]})"
            data = json.loads(report.read_text() or "[]")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            return [], f"error ({type(exc).__name__})"

    findings = []
    for leak in data or []:
        path = _relpath(str(leak.get("File", "")), cfg.workspace)
        if path not in tracked:  # ignore untracked build/dependency folders
            continue
        rule = leak.get("RuleID", "secret")
        desc = leak.get("Description") or rule
        start = int(leak.get("StartLine") or 1)
        findings.append(
            Finding(
                source="gitleaks",
                category="secret",
                severity="critical",
                path=path,
                start_line=start,
                end_line=int(leak.get("EndLine") or start),
                title=f"Possible secret committed ({rule})",
                rule_id=f"gitleaks:{rule}",
                verified=True,
                explanation=(
                    f"Gitleaks rule `{rule}` matched this line. {desc} "
                    "The value is redacted in this report and was not sent to any AI model."
                ),
                why=(
                    "Anyone who can read the repository, including its history, can use a committed "
                    "credential. Deleting it in a later commit does not remove it from history."
                ),
                suggested_change=(
                    "Revoke or rotate the credential first. Then remove it from the code and load it from "
                    "an environment variable or secret store. If the repository is or was public, purge it "
                    "from history (for example with git filter-repo). If this is a harmless test fixture, "
                    "add a `gitleaks:allow` comment on that line or list the finding in `.gitleaksignore`."
                ),
            )
        )
    return findings, f"ran ({len(findings)} results)"


def _cvss_to_severity(score: float) -> str:
    if score >= 9:
        return "critical"
    if score >= 7:
        return "high"
    if score >= 4:
        return "medium"
    return "low" if score > 0 else "medium"


def osv(cfg: Config, all_paths: list[str] | None = None) -> tuple[list[Finding], str]:
    if not shutil.which("osv-scanner"):
        return [], "skipped (not installed)"
    try:
        proc = _run(["osv-scanner", "scan", "source", "-r", "--format", "json", "."], cfg.workspace, timeout=900)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [], f"error ({type(exc).__name__})"
    if proc.returncode == 128:
        return [], "ran (no supported lockfiles or manifests found)"
    if proc.returncode not in (0, 1):
        return [], f"error (exit {proc.returncode}: {proc.stderr.strip()[:200]})"
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return [], "error (unreadable output)"

    tracked = set(all_paths) if all_paths is not None else None
    findings = []
    for result in data.get("results") or []:
        source = _relpath(str((result.get("source") or {}).get("path", "")), cfg.workspace)
        if tracked is not None and source not in tracked:
            continue  # e.g. node_modules, or a checked-out copy of this tool
        for pkg in result.get("packages") or []:
            info = pkg.get("package") or {}
            vulns = pkg.get("vulnerabilities") or []
            if not vulns:
                continue
            name, version, eco = info.get("name", "?"), info.get("version", "?"), info.get("ecosystem", "")
            scores = []
            for group in pkg.get("groups") or []:
                try:
                    scores.append(float(group.get("max_severity") or 0))
                except (TypeError, ValueError):
                    pass
            fixed = sorted({
                event["fixed"]
                for v in vulns
                for affected in v.get("affected") or []
                if (affected.get("package") or {}).get("name") == name
                for rng in affected.get("ranges") or []
                for event in rng.get("events") or []
                if "fixed" in event
            })
            ids = sorted({v.get("id", "") for v in vulns if v.get("id")})
            lines = [f"- `{v.get('id')}`: {v.get('summary') or 'no summary'}" for v in vulns[:10]]
            if len(vulns) > 10:
                lines.append(f"- ...and {len(vulns) - 10} more")
            upgrade = f" ({', '.join(fixed[:6])})" if fixed else ""
            findings.append(
                Finding(
                    source="osv",
                    category="dependency",
                    severity=_cvss_to_severity(max(scores) if scores else 0),
                    path=source,
                    start_line=0,
                    end_line=0,
                    title=f"Vulnerable dependency {name}@{version} ({len(ids)} known advisories)",
                    rule_id=f"osv:{eco}:{name}:{version}",
                    verified=True,
                    explanation=f"{eco} package `{name}` version `{version}` has known vulnerabilities:\n" + "\n".join(lines),
                    why="Known vulnerabilities in dependencies are among the easiest things for attackers to find and exploit.",
                    suggested_change=(
                        f"Upgrade `{name}` to a patched version{upgrade}, then regenerate the lockfile "
                        "and run the tests. If an upgrade is not possible yet, check whether the vulnerable "
                        "code path is reachable and document the decision."
                    ),
                    references=[f"https://osv.dev/vulnerability/{i}" for i in ids[:5]],
                )
            )
    return findings, f"ran ({len(findings)} vulnerable packages)"


def run_all(cfg: Config, files: dict[str, FileInfo], all_paths: list[str]) -> tuple[list[Finding], list[Finding], dict[str, str]]:
    """Returns (candidates for AI verification, deterministic findings, tool status)."""
    semgrep_findings, s1 = semgrep(cfg, files)
    secret_findings, s2 = gitleaks(cfg, all_paths)
    dep_findings, s3 = osv(cfg, all_paths)
    status = {"Semgrep": s1, "Gitleaks": s2, "OSV-Scanner": s3}
    return semgrep_findings, secret_findings + dep_findings, status
