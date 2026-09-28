"""Core data types shared by every stage of the pipeline."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field, fields

SEVERITIES = ["low", "medium", "high", "critical"]
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

CATEGORIES = [
    "bug",
    "broken-code",
    "error-handling",
    "security",
    "secret",
    "dependency",
    "performance",
    "unused-code",
    "dead-code",
    "duplication",
    "concurrency",
    "type-safety",
    "config",
    "doc-drift",
    "maintainability",
]

_SEVERITY_ALIASES = {
    "info": "low",
    "minor": "low",
    "trivial": "low",
    "warning": "medium",
    "moderate": "medium",
    "major": "high",
    "error": "high",
    "blocker": "critical",
}


def norm_severity(value: object, default: str = "medium") -> str:
    v = str(value or "").strip().lower()
    v = _SEVERITY_ALIASES.get(v, v)
    return v if v in SEVERITY_RANK else default


def norm_category(value: object, default: str = "bug") -> str:
    v = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if v in CATEGORIES:
        return v
    if v in {"unused", "unused-import", "unused-variable"}:
        return "unused-code"
    if v in {"docs", "documentation", "doc", "docs-drift"}:
        return "doc-drift"
    if v in {"perf", "optimization", "optimisation"}:
        return "performance"
    return "maintainability" if v else default


@dataclass
class FileInfo:
    path: str
    text: str
    sha: str
    language: str
    is_doc: bool

    @property
    def line_count(self) -> int:
        return self.text.count("\n") + 1


@dataclass
class Finding:
    source: str  # gemini | docs | semgrep | gitleaks | osv | links
    category: str
    severity: str
    path: str
    start_line: int
    end_line: int
    title: str
    description: str = ""
    rule_id: str = ""
    related_files: list[str] = field(default_factory=list)
    # Filled in by verification (Claude or a second Gemini pass) or directly by deterministic scanners.
    verified: bool = False
    explanation: str = ""
    why: str = ""
    suggested_change: str = ""
    edits: list[dict] = field(default_factory=list)
    related_updates: list[dict] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    also_at: list[str] = field(default_factory=list)
    # Filled in later in the pipeline.
    fingerprint: str = ""
    issue_number: int | None = None
    issue_url: str = ""
    fix_applied: bool = False

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 1)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Finding":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def compute_fingerprint(finding: Finding, file_text: str | None) -> str:
    """A stable identity for a finding, used to avoid duplicate issues.

    It hashes the path, the rule (for scanner findings) and the flagged code with
    all whitespace removed, so it survives line shifts and reformatting but changes
    when the problematic code itself changes.
    """
    basis = ""
    if file_text is not None and finding.start_line > 0:
        lines = file_text.splitlines()
        end = max(finding.end_line, finding.start_line)
        basis = re.sub(r"\s+", "", "".join(lines[finding.start_line - 1 : end]))
    if not basis:
        basis = re.sub(r"\W+", "", finding.title.lower())
    key = "|".join([finding.path, finding.rule_id or "", basis])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
