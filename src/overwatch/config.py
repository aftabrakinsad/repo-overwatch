"""Runtime configuration: action inputs, the GitHub event context and .overwatch.yml."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml

from .models import SEVERITY_RANK


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if str(v).strip()]


def git(workspace: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _repo_from_remote(workspace: Path) -> str:
    url = git(workspace, "remote", "get-url", "origin")
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else "local/repository"


@dataclass
class Config:
    workspace: Path
    output_dir: Path
    cache_dir: Path
    repo: str
    server_url: str
    api_url: str
    event_name: str
    branch: str
    sha: str
    default_branch: str
    pr_number: int | None
    reply_to: int | None
    requested_by: str
    is_fork_pr: bool
    skip_reason: str
    run_url: str
    in_actions: bool
    github_token: str
    gemini_api_key: str
    anthropic_api_key: str
    gemini_model: str
    claude_model: str
    verifier: str
    gemini_verify_model: str
    gemini_rpm: int
    gemini_tpm: int
    max_ai_requests: int
    dry_run: bool
    create_issues: bool
    create_fix_pr: bool
    fix_pr_draft: bool
    max_new_issues: int
    min_severity: str
    fail_on: str
    exclude: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    validate: list[str] = field(default_factory=list)
    max_file_bytes: int = 200_000
    max_files: int = 2000
    batch_chars: int = 250_000
    concurrency: int = 4
    semgrep_config: str = "p/default"
    verifier_label: str = "Claude"

    @property
    def is_default_branch(self) -> bool:
        return self.branch == self.default_branch

    @property
    def short_sha(self) -> str:
        return self.sha[:7] if self.sha else "unknown"

    def permalink(self, path: str, start: int = 0, end: int = 0) -> str:
        url = f"{self.server_url}/{self.repo}/blob/{self.sha or self.branch}/{quote(path)}"
        if start > 0:
            url += f"#L{start}"
            if end > start:
                url += f"-L{end}"
        return url


def own_files(workspace: Path, config_file: Path) -> list[str]:
    """Overwatch's own files in the scanned repository, which it should not review:
    the workflow file that started this run (from GITHUB_WORKFLOW_REF, e.g.
    "owner/repo/.github/workflows/overwatch.yml@refs/heads/main") and the config file."""
    paths = []
    match = re.match(r"^[^/]+/[^/]+/(.+?)@", _env("GITHUB_WORKFLOW_REF"))
    if match:
        paths.append(match.group(1))
    try:
        paths.append(config_file.resolve().relative_to(workspace).as_posix())
    except ValueError:
        pass
    # Leading "/" anchors each pattern to that exact file (gitignore syntax).
    return ["/" + p for p in paths]


def load(workspace: Path, dry_run: bool | None = None, config_path: str | None = None) -> Config:
    ws = workspace.resolve()

    event: dict = {}
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).is_file():
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            event = {}

    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    event_name = _env("GITHUB_EVENT_NAME", default="local")
    repo = _env("GITHUB_REPOSITORY") or _repo_from_remote(ws)
    current = git(ws, "rev-parse", "--abbrev-ref", "HEAD")
    branch = (
        _env("OVERWATCH_BRANCH", "GITHUB_HEAD_REF")
        or (current if current and current != "HEAD" else "")
        or _env("GITHUB_REF_NAME")
        or "HEAD"
    )
    sha = git(ws, "rev-parse", "HEAD") or _env("GITHUB_SHA")
    default_branch = (event.get("repository") or {}).get("default_branch") or _env(
        "OVERWATCH_DEFAULT_BRANCH", default="main"
    )

    pr = event.get("pull_request") or {}
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    is_fork_pr = bool(pr) and head_repo != base_repo

    skip_reason = ""
    if event_name == "create" and event.get("ref_type") != "branch":
        skip_reason = "a tag was created (only branches are scanned)"
    elif branch.startswith("overwatch/"):
        skip_reason = "this is Overwatch's own fix branch"

    file_cfg: dict = {}
    cfg_file = ws / (config_path or _env("OVERWATCH_CONFIG_PATH", default=".overwatch.yml"))
    if cfg_file.is_file():
        loaded = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            file_cfg = loaded

    def setting(key: str, env_name: str, default: object) -> object:
        """The repository's .overwatch.yml wins over workflow inputs."""
        if key in file_cfg and file_cfg[key] is not None:
            return file_cfg[key]
        return _env(env_name, default=str(default)) if not isinstance(default, bool) else _env(env_name)

    min_severity = str(setting("min_severity", "OVERWATCH_MIN_SEVERITY", "low")).lower()
    if min_severity not in SEVERITY_RANK:
        min_severity = "low"
    fail_on = str(setting("fail_on", "OVERWATCH_FAIL_ON", "none")).lower()
    if fail_on not in SEVERITY_RANK:
        fail_on = "none"

    if dry_run is None:
        dry_run = _bool(_env("OVERWATCH_DRY_RUN"), default=not in_actions)

    default_out = Path(tempfile.gettempdir()) / "overwatch-output"
    default_cache = Path(tempfile.gettempdir()) / "overwatch-cache"
    fix_pr_cfg = file_cfg.get("fix_pr") if isinstance(file_cfg.get("fix_pr"), dict) else {}

    run_id = _env("GITHUB_RUN_ID")
    server_url = _env("GITHUB_SERVER_URL", default="https://github.com")

    return Config(
        workspace=ws,
        output_dir=Path(_env("OVERWATCH_OUTPUT_DIR", default=str(default_out))),
        cache_dir=Path(_env("OVERWATCH_CACHE_DIR", default=str(default_cache))),
        repo=repo,
        server_url=server_url,
        api_url=_env("GITHUB_API_URL", default="https://api.github.com"),
        event_name=event_name,
        branch=branch,
        sha=sha,
        default_branch=default_branch,
        pr_number=pr.get("number"),
        reply_to=(event.get("issue") or {}).get("number") if event_name == "issue_comment" else None,
        requested_by=((event.get("comment") or {}).get("user") or {}).get("login", ""),
        is_fork_pr=is_fork_pr,
        skip_reason=skip_reason,
        run_url=f"{server_url}/{repo}/actions/runs/{run_id}" if run_id else "",
        in_actions=in_actions,
        github_token=_env("OVERWATCH_GITHUB_TOKEN", "GITHUB_TOKEN"),
        gemini_api_key=_env("OVERWATCH_GEMINI_API_KEY", "GEMINI_API_KEY"),
        anthropic_api_key=_env("OVERWATCH_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        gemini_model=str(setting("gemini_model", "OVERWATCH_GEMINI_MODEL", "gemini-3.5-flash-lite")),
        claude_model=str(setting("claude_model", "OVERWATCH_CLAUDE_MODEL", "claude-sonnet-5")),
        verifier=str(setting("verifier", "OVERWATCH_VERIFIER", "auto")).lower(),
        gemini_verify_model=str(setting("gemini_verify_model", "OVERWATCH_GEMINI_VERIFY_MODEL", "")),
        gemini_rpm=max(0, _int(setting("gemini_rpm", "OVERWATCH_GEMINI_RPM", 10), 10)),
        gemini_tpm=max(0, _int(setting("gemini_tpm", "OVERWATCH_GEMINI_TPM", 200_000), 200_000)),
        max_ai_requests=max(0, _int(setting("max_ai_requests", "OVERWATCH_MAX_AI_REQUESTS", 100), 100)),
        dry_run=dry_run,
        create_issues=_bool(setting("create_issues", "OVERWATCH_CREATE_ISSUES", True), True),
        create_fix_pr=_bool(
            fix_pr_cfg.get("enabled") if "enabled" in fix_pr_cfg else _env("OVERWATCH_CREATE_FIX_PR"),
            True,
        ),
        fix_pr_draft=_bool(fix_pr_cfg.get("draft"), False),
        max_new_issues=_int(setting("max_new_issues", "OVERWATCH_MAX_NEW_ISSUES", 20), 20),
        min_severity=min_severity,
        fail_on=fail_on,
        exclude=_str_list(file_cfg.get("exclude")) + own_files(ws, cfg_file),
        include=_str_list(file_cfg.get("include")),
        docs=_str_list(file_cfg.get("docs")),
        validate=_str_list(file_cfg.get("validate")),
        max_file_bytes=_int(file_cfg.get("max_file_bytes"), 200_000),
        max_files=_int(file_cfg.get("max_files"), 2000),
        batch_chars=_int(file_cfg.get("batch_chars"), 250_000),
        concurrency=max(1, _int(file_cfg.get("concurrency"), 2)),
        semgrep_config=str(file_cfg.get("semgrep_config") or "p/default"),
    )
