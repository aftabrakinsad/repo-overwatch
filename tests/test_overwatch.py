"""Tests for Repo Overwatch. Run with: PYTHONPATH=src pytest -q"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from overwatch import config, depgraph, fixer, issues, pipeline, repo
from overwatch.llm import parse_json
from overwatch.models import FileInfo, Finding, compute_fingerprint


# ------------------------------------------------------------------ helpers
def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return tmp_path


def load_cfg(ws: Path, monkeypatch, **env) -> config.Config:
    for key in list(os.environ):
        if key.startswith(("GITHUB_", "OVERWATCH_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OVERWATCH_OUTPUT_DIR", str(ws.parent / f"{ws.name}-out"))
    monkeypatch.setenv("OVERWATCH_CACHE_DIR", str(ws.parent / f"{ws.name}-cache"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return config.load(ws, dry_run=True)


SAMPLE = {
    "src/lib/math.ts": "export function add(a: number, b: number) {\n  return a - b;\n}\n",
    "src/main.ts": "import { add } from './lib/math';\nconsole.log(add(1, 2));\n",
    "README.md": "# Demo\n\nRun `npm start`. See [guide](docs/guide.md) and [missing](docs/missing.md).\n",
    "docs/guide.md": "Uses `src/main.ts`.\n",
    "package.json": '{"name": "demo", "scripts": {"dev": "vite"}}\n',
    ".overwatch.yml": "validate:\n  - grep -q 'a + b' src/lib/math.ts\n",
}


class FakeGemini:
    def __init__(self):
        self.calls = 0

    def json(self, system, prompt):
        self.calls += 1
        if "=== DOC:" in prompt:
            return {"findings": [{"path": "README.md", "start_line": 3, "end_line": 3, "category": "doc-drift",
                                  "severity": "high", "title": "README says npm start but no start script exists",
                                  "description": "package.json only defines dev.", "related_files": ["package.json"]}]}
        return {"findings": [{"path": "src/lib/math.ts", "start_line": 2, "end_line": 2, "category": "bug",
                              "severity": "high", "title": "add() subtracts", "description": "Uses minus.",
                              "related_files": ["src/main.ts"]},
                             {"path": "not/in/batch.ts", "start_line": 1, "end_line": 1, "title": "ignored"}]}


class FakeClaude:
    """Verifier stand-in: reads the candidate list from the prompt and judges each by title."""

    def __init__(self):
        self.calls = 0

    def json(self, system, prompt):
        self.calls += 1
        candidates = json.loads(prompt.split("Candidate findings:\n", 1)[1])
        results = []
        for c in candidates:
            cid = c["id"]
            if c["title"] == "add() subtracts":
                results.append({"id": cid, "verdict": "confirmed", "severity": "high", "category": "bug",
                                "title": "add() returns a - b instead of a + b", "start_line": 2, "end_line": 2,
                                "explanation": "The function named add subtracts.", "why_it_matters": "Every caller gets wrong sums.",
                                "suggested_change": "Use +.", "edits": [{"path": "src/lib/math.ts", "old": "  return a - b;", "new": "  return a + b;"}],
                                "related_updates": [{"path": "src/main.ts", "reason": "Its printed output changes."}]})
            elif "npm start" in c["title"]:
                results.append({"id": cid, "verdict": "confirmed", "explanation": "No start script.", "edits": []})
            else:
                results.append({"id": cid, "verdict": "rejected", "reason": "not a problem"})
        return {"results": results}


class FakeGeminiOnly(FakeGemini):
    """One Gemini key does both passes: analysis prompts and verification prompts."""

    def __init__(self):
        super().__init__()
        self.verifier = FakeClaude()

    def json(self, system, prompt):
        if "Candidate findings:" in prompt:
            self.calls += 1
            return self.verifier.json(system, prompt)
        return super().json(system, prompt)


class FakeGitHub:
    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.comments: list[tuple[int, str]] = []
        self.next = 1

    def ensure_labels(self, labels):
        pass

    def list_issues(self, label, state="all"):
        out = []
        for i in self.issues.values():
            names = {l["name"] for l in i["labels"]}
            if label in names and (state == "all" or i["state"] == state):
                out.append(dict(i))
        return out

    def create_issue(self, title, body, labels):
        n = self.next
        self.next += 1
        self.issues[n] = {"number": n, "title": title, "body": body, "state": "open", "state_reason": None,
                          "labels": [{"name": l} for l in labels], "html_url": f"https://x/issues/{n}"}
        return self.issues[n]

    def update_issue(self, number, **fields):
        issue = self.issues[number]
        if "labels" in fields:
            fields["labels"] = [{"name": l} for l in fields["labels"]]
        issue.update(fields)
        return issue

    def comment(self, number, body):
        self.comments.append((number, body))


# ------------------------------------------------------------------ unit tests
def test_parse_json_handles_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"findings": []} done') == {"findings": []}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_fingerprint_survives_line_shifts_but_not_code_changes():
    f = Finding("gemini", "bug", "high", "a.py", 2, 2, "t")
    base = compute_fingerprint(f, "x = 1\nreturn a - b\n")
    shifted = Finding("gemini", "bug", "high", "a.py", 3, 3, "t")
    assert compute_fingerprint(shifted, "\nx = 1\n    return a - b\n") == base
    assert compute_fingerprint(f, "x = 1\nreturn a + b\n") != base


def test_language_detection_and_dotdirs():
    assert repo.detect_language(".github/workflows/ci.yml") == "yaml"
    assert repo.detect_language("notes.txt") == ""
    assert repo.detect_language("docs/notes.txt") == "text"
    assert repo.detect_language("Dockerfile.prod") == "dockerfile"
    assert depgraph._norm("./.github/x.yml") == ".github/x.yml"


def test_dependency_graph_across_languages():
    files = {
        "src/lib/util.ts": "export const x = 1;\n",
        "src/routes/page.svelte": "<script>import { x } from '$lib/util';</script>\n",
        "src/app.ts": "import { x } from './lib/util.js';\n",
        "pkg/__init__.py": "",
        "pkg/core.py": "from .helpers import go\n",
        "pkg/helpers.py": "def go(): pass\n",
        "tool.py": "from pkg import core\n",
        "Cargo.toml": "[package]\n",
        "src/main.rs": "mod net;\nuse crate::net::client;\n",
        "src/net/mod.rs": "pub mod client;\n",
        "src/net/client.rs": "pub fn get() {}\n",
    }
    infos = {p: FileInfo(p, t, "0", repo.detect_language(p), False) for p, t in files.items()}
    g = depgraph.build(infos, list(files))
    assert "src/lib/util.ts" in g.imports["src/routes/page.svelte"]
    assert "src/lib/util.ts" in g.imports["src/app.ts"]  # .js specifier resolves to .ts
    assert "pkg/helpers.py" in g.imports["pkg/core.py"]
    assert "pkg/core.py" in g.imports["tool.py"]
    assert "src/net/mod.rs" in g.imports["src/main.rs"]
    assert "src/net/client.rs" in g.imports["src/net/mod.rs"]
    assert "src/app.ts" in g.dependents["src/lib/util.ts"]


def test_broken_links_ignore_code_blocks_and_urls():
    text = "[ok](a.md)\n[gone](b.md)\n```\n[inside](nope.md)\n```\n[web](https://x.y) `[code](z.md)`\n"
    files = {"README.md": FileInfo("README.md", text, "0", "markdown", True)}
    found = depgraph.broken_links(files, ["README.md", "a.md"])
    assert [(f.start_line, "b.md" in f.title) for f in found] == [(2, True)]


def test_apply_edits_is_exact_and_all_or_nothing(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 1\n")
    good = Finding("gemini", "bug", "high", "a.py", 1, 1, "g", edits=[{"path": "a.py", "old": "x = 1", "new": "x = 2"}])
    ambiguous = Finding("gemini", "bug", "low", "a.py", 1, 1, "a", edits=[{"path": "a.py", "old": " = ", "new": " := "}])
    workflow = Finding("gemini", "config", "low", ".github/workflows/ci.yml", 1, 1, "w",
                       edits=[{"path": ".github/workflows/ci.yml", "old": "a", "new": "b"}])
    applied, failed, contents = fixer.apply_edits(tmp_path, [good, ambiguous, workflow], {"a.py", ".github/workflows/ci.yml"})
    assert applied == [good]
    assert {f.title for f, _ in failed} == {"a", "w"}
    assert (tmp_path / "a.py").read_text() == "x = 2\ny = 1\n"


def test_apply_edits_strips_line_number_prefixes(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    f = Finding("gemini", "bug", "high", "a.py", 2, 2, "t",
                edits=[{"path": "a.py", "old": "    2|     return 1", "new": "    2|     return 2"}])
    applied, _, _ = fixer.apply_edits(tmp_path, [f], {"a.py"})
    assert applied and (tmp_path / "a.py").read_text().endswith("return 2\n")


# ------------------------------------------------------------------ end to end
def test_end_to_end_dry_run(tmp_path, monkeypatch):
    ws = make_repo(tmp_path / "repo", SAMPLE)
    cfg = load_cfg(ws, monkeypatch)
    gemini, claude = FakeGemini(), FakeClaude()
    assert pipeline.main(cfg, gemini=gemini, claude=claude) == 0

    report_md = (cfg.output_dir / "overwatch-report.md").read_text()
    assert "add() returns a - b instead of a + b" in report_md
    assert "README says" not in report_md or "No start script" in report_md
    assert "Broken link to `docs/missing.md`" in report_md
    assert "src/main.ts" in report_md  # linked file that also needs updating
    patch = (cfg.output_dir / "overwatch-fixes.patch").read_text()
    assert "+  return a + b;" in patch
    # The workspace is restored to the scanned commit after validation.
    assert (ws / "src/lib/math.ts").read_text() == SAMPLE["src/lib/math.ts"]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=ws, capture_output=True, text=True).stdout == ""

    # Second run: everything comes from the cache, no new model calls.
    g_calls, c_calls = gemini.calls, claude.calls
    cfg2 = load_cfg(ws, monkeypatch)
    assert pipeline.main(cfg2, gemini=gemini, claude=claude) == 0
    assert (gemini.calls, claude.calls) == (g_calls, c_calls)


def test_fail_on_threshold(tmp_path, monkeypatch):
    ws = make_repo(tmp_path / "repo", SAMPLE)
    cfg = load_cfg(ws, monkeypatch, OVERWATCH_FAIL_ON="high")
    assert pipeline.main(cfg, gemini=FakeGemini(), claude=FakeClaude()) == 1


def test_issue_sync_dedupes_respects_dismissal_and_closes(tmp_path, monkeypatch):
    ws = make_repo(tmp_path / "repo", SAMPLE)
    cfg = load_cfg(ws, monkeypatch)
    gh = FakeGitHub()
    a = Finding("gemini", "bug", "high", "src/lib/math.ts", 2, 2, "A", fingerprint="a" * 16)
    b = Finding("gemini", "bug", "low", "src/main.ts", 1, 1, "B", fingerprint="b" * 16)

    r1 = issues.sync(cfg, gh, [a, b], {a.fingerprint, b.fingerprint}, True)
    assert len(r1.created) == 2 and a.issue_number == 1

    r2 = issues.sync(cfg, gh, [a, b], {a.fingerprint, b.fingerprint}, True)
    assert r2.created == [] and r2.updated == []  # identical content: no churn

    gh.update_issue(2, state="closed", state_reason="not_planned")
    r3 = issues.sync(cfg, gh, [a], {a.fingerprint}, True)
    assert r3.closed == [] and gh.issues[2]["state"] == "closed"  # dismissed stays closed

    r4 = issues.sync(cfg, gh, [b], {b.fingerprint}, True)
    assert r4.closed == [1] and r4.dismissed == 1  # a is gone -> closed as fixed

    r5 = issues.sync(cfg, gh, [a], {a.fingerprint}, True)
    assert r5.reopened == [1]  # a came back

    cfg.max_new_issues = 0
    c = Finding("gemini", "bug", "medium", "x", 1, 1, "C", fingerprint="c" * 16)
    assert issues.sync(cfg, gh, [c], {c.fingerprint}, True).overflow == [c]


def test_skip_own_fix_branch(tmp_path, monkeypatch):
    ws = make_repo(tmp_path / "repo", SAMPLE)
    cfg = load_cfg(ws, monkeypatch, OVERWATCH_BRANCH="overwatch/fixes-main")
    assert cfg.skip_reason
    assert pipeline.main(cfg, gemini=FakeGemini(), claude=FakeClaude()) == 0


def test_osv_output_parsing(tmp_path, monkeypatch):
    from overwatch import scanners

    ws = make_repo(tmp_path / "repo", {"package-lock.json": "{}"})
    cfg = load_cfg(ws, monkeypatch)
    output = {
        "results": [{
            "source": {"path": str(ws / "package-lock.json"), "type": "lockfile"},
            "packages": [{
                "package": {"name": "lodash", "version": "4.17.20", "ecosystem": "npm"},
                "groups": [{"ids": ["GHSA-1"], "max_severity": "7.4"}],
                "vulnerabilities": [{
                    "id": "GHSA-1", "summary": "Prototype pollution",
                    "affected": [{"package": {"name": "lodash"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "4.17.21"}]}]}],
                }],
            }],
        }]
    }

    class Proc:
        returncode, stdout, stderr = 1, __import__("json").dumps(output), ""

    monkeypatch.setattr(scanners.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(scanners, "_run", lambda *a, **k: Proc())
    findings, status = scanners.osv(cfg, ["package-lock.json"])
    assert status.startswith("ran")
    assert scanners.osv(cfg, ["other.txt"])[0] == []  # untracked lockfiles are ignored
    [f] = findings
    assert (f.path, f.severity, f.category) == ("package-lock.json", "high", "dependency")
    assert "4.17.21" in f.suggested_change and f.verified


class FakePRGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.prs: list[dict] = []
        self.reviews: list[tuple[int, list[dict]]] = []

    def find_open_pr(self, head_branch, base):
        return next((p for p in self.prs if p["head"] == head_branch and p["base"] == base), None)

    def create_pr(self, title, body, head, base, draft=False):
        pr = {"number": 100 + len(self.prs), "html_url": f"https://x/pull/{100 + len(self.prs)}",
              "title": title, "body": body, "head": head, "base": base}
        self.prs.append(pr)
        return pr

    def update_pr(self, number, **fields):
        pr = next(p for p in self.prs if p["number"] == number)
        pr.update(fields)
        return pr

    def create_review(self, number, body, comments):
        self.reviews.append((number, comments))

    def ensure_labels(self, labels):
        pass

    def list_comments(self, number):
        return []


def test_fix_pr_pushes_once_and_restores_workspace(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    ws = make_repo(tmp_path / "repo", SAMPLE)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=ws, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=ws, check=True)

    cfg = load_cfg(ws, monkeypatch)
    cfg.dry_run = False
    gh = FakePRGitHub()
    assert pipeline.main(cfg, gemini=FakeGemini(), claude=FakeClaude(), gh=gh) == 0

    [pr] = gh.prs
    assert pr["head"] == "overwatch/fixes-main" and pr["base"] == "main"
    assert "Fixes #" in pr["body"] and "Passed" in pr["body"]
    [(number, comments)] = gh.reviews
    assert comments[0]["path"] == "src/lib/math.ts" and comments[0]["line"] == 2
    pushed = subprocess.run(["git", "--git-dir", str(remote), "show", "overwatch/fixes-main:src/lib/math.ts"],
                            capture_output=True, text=True, check=True).stdout
    assert "a + b" in pushed
    assert subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ws, capture_output=True, text=True).stdout.strip() == "main"
    assert (ws / "src/lib/math.ts").read_text() == SAMPLE["src/lib/math.ts"]

    # Re-running with identical fixes must not push again or post duplicate reviews.
    cfg2 = load_cfg(ws, monkeypatch)
    cfg2.dry_run = False
    assert pipeline.main(cfg2, gemini=FakeGemini(), claude=FakeClaude(), gh=gh) == 0
    assert len(gh.prs) == 1 and len(gh.reviews) == 1


def test_comment_command_scans_requested_branch_and_replies(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    ws = make_repo(tmp_path / "repo", SAMPLE)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=ws, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=ws, check=True)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({
        "issue": {"number": 7},
        "comment": {"id": 1, "body": "run overwatch", "user": {"login": "alice"}},
        "repository": {"default_branch": "main"},
    }))
    cfg = load_cfg(ws, monkeypatch, GITHUB_EVENT_NAME="issue_comment", GITHUB_EVENT_PATH=str(event),
                   GITHUB_REF_NAME="main", OVERWATCH_BRANCH="main")
    cfg.dry_run = False
    assert (cfg.branch, cfg.reply_to, cfg.requested_by) == ("main", 7, "alice")

    gh = FakePRGitHub()
    assert pipeline.main(cfg, gemini=FakeGemini(), claude=FakeClaude(), gh=gh) == 0
    replies = [body for number, body in gh.comments if number == 7]
    assert len(replies) == 1 and replies[0].startswith("@alice Scan finished.")
    assert "add() returns a - b instead of a + b" in replies[0] and "https://x/pull/" in replies[0]


def test_gemini_only_mode_verifies_with_second_gemini_pass(tmp_path, monkeypatch):
    ws = make_repo(tmp_path / "repo", SAMPLE)
    cfg = load_cfg(ws, monkeypatch)
    gemini = FakeGeminiOnly()
    assert pipeline.main(cfg, gemini=gemini) == 0
    report_md = (cfg.output_dir / "overwatch-report.md").read_text()
    assert "add() returns a - b instead of a + b" in report_md
    assert "verified by a second Gemini pass" in report_md and "Claude" not in report_md
    assert gemini.verifier.calls == 1  # both files' candidates verified in ONE batched request


def test_quota_stop_keeps_progress_and_resumes(tmp_path, monkeypatch):
    from overwatch.llm import QuotaExhausted

    class DailyQuotaGemini(FakeGeminiOnly):
        """Allows a fixed number of requests, then behaves like an exhausted free tier."""

        def __init__(self, allowed):
            super().__init__()
            self.allowed = allowed

        def json(self, system, prompt):
            if self.allowed <= 0:
                raise QuotaExhausted("the Gemini quota for `gemini-3.5-flash-lite` is used up for today")
            self.allowed -= 1
            return super().json(system, prompt)

    ws = make_repo(tmp_path / "repo", SAMPLE)
    cfg = load_cfg(ws, monkeypatch)
    first = DailyQuotaGemini(allowed=1)  # only the code pass succeeds
    assert pipeline.main(cfg, gemini=first) == 0
    report_md = (cfg.output_dir / "overwatch-report.md").read_text()
    assert "stopped early" in report_md.lower() and "run overwatch" in report_md

    second = DailyQuotaGemini(allowed=10)  # the next day
    assert pipeline.main(load_cfg(ws, monkeypatch), gemini=second) == 0
    report_md = (cfg.output_dir / "overwatch-report.md").read_text()
    assert "add() returns a - b instead of a + b" in report_md and "stopped early" not in report_md.lower()
    assert second.calls == 2  # docs pass + verification; the finished code pass came from the cache


def test_rate_limiter_budget_and_429_classification():
    from overwatch.llm import Budget, QuotaExhausted, RateLimiter, classify_rate_limit

    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(rpm=2, tpm=0, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.acquire(100)
    assert slept and sum(slept) >= 60  # third request waited for the one-minute window

    tokens = RateLimiter(rpm=0, tpm=1000, clock=lambda: now[0], sleep=sleep)
    slept.clear()
    tokens.acquire(800)
    tokens.acquire(800)
    assert sum(slept) >= 60

    budget = Budget(2)
    budget.take()
    budget.take()
    with pytest.raises(QuotaExhausted):
        budget.take()

    assert classify_rate_limit("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier") [0]
    assert classify_rate_limit("Quota exceeded for metric ... limit: 0, model: gemini-x")[0]
    daily, wait = classify_rate_limit("429 RESOURCE_EXHAUSTED PerMinute ... Please retry in 37.4s. 'retryDelay': '37s'")
    assert not daily and wait == 37.4


def test_gemini_client_handles_free_tier_429s(monkeypatch):
    from overwatch import llm

    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    class RateLimitError(Exception):
        code = 429

    class Response:
        text = '{"findings": []}'
        usage_metadata = None

    class Models:
        def __init__(self, errors):
            self.errors, self.calls = list(errors), 0

        def generate_content(self, **kwargs):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
            return Response()

    budget = llm.Budget(10)
    client = llm.GeminiClient("fake-key", "gemini-3.5-flash-lite", llm.RateLimiter(0, 0), budget)

    # A per-minute limit is waited out and retried.
    class Inner:
        def __init__(self, errors):
            self.models = Models(errors)

    client.client = Inner([RateLimitError("429 RESOURCE_EXHAUSTED PerMinute. Please retry in 1s.")])
    assert client.json("sys", "prompt") == {"findings": []}
    assert client.client.models.calls == 2

    # A daily limit stops the run: no retries, and every later call fails fast.
    client.client = Inner([RateLimitError("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")])
    with pytest.raises(llm.QuotaExhausted):
        client.json("sys", "prompt")
    assert client.client.models.calls == 1
    with pytest.raises(llm.QuotaExhausted):
        client.json("sys", "prompt")
    assert client.client.models.calls == 1


def test_rate_limiter_backoff_waits_only_as_long_as_asked():
    from overwatch.llm import RateLimiter

    now, slept = [0.0], []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(rpm=10, tpm=0, clock=lambda: now[0], sleep=sleep)
    limiter.acquire(10)
    limiter.block(5)  # a 429 said "retry in 5s"
    limiter.acquire(10)
    assert 5 <= sum(slept) < 7


def test_private_tool_checked_out_inside_workspace_is_ignored(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    ws = make_repo(tmp_path / "repo", SAMPLE)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=ws, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=ws, check=True)
    # Simulate the workflow's "Download the private Repo Overwatch tool" step.
    make_repo(ws / ".overwatch-action", {"action.yml": "name: x\n", "src/overwatch/tool.py": "import os\n",
                                         "requirements.txt": "requests==2.0.0\n"})

    cfg = load_cfg(ws, monkeypatch)
    files, _, _ = repo.collect(cfg)
    assert not any(p.startswith(".overwatch-action") for p in files)

    cfg.dry_run = False
    gh = FakePRGitHub()
    assert pipeline.main(cfg, gemini=FakeGemini(), claude=FakeClaude(), gh=gh) == 0
    report_md = (cfg.output_dir / "overwatch-report.md").read_text()
    assert ".overwatch-action" not in report_md
    [pr] = gh.prs
    changed = subprocess.run(["git", "--git-dir", str(remote), "diff", "--name-only", "main", "overwatch/fixes-main"],
                             capture_output=True, text=True, check=True).stdout.split()
    assert changed == ["src/lib/math.ts"]
    assert (ws / ".overwatch-action" / "action.yml").exists()  # the tool is left intact


def test_overwatch_skips_its_own_workflow_and_config(tmp_path, monkeypatch):
    files = dict(SAMPLE)
    files[".github/workflows/overwatch.yml"] = "name: Repo Overwatch\n"
    files[".github/workflows/ci.yml"] = "name: CI\n"
    ws = make_repo(tmp_path / "repo", files)
    cfg = load_cfg(ws, monkeypatch,
                   GITHUB_WORKFLOW_REF="someone/app/.github/workflows/overwatch.yml@refs/heads/master")
    analyzed, _, _ = repo.collect(cfg)
    assert ".github/workflows/overwatch.yml" not in analyzed  # the workflow that runs Overwatch
    assert ".overwatch.yml" not in analyzed                   # Overwatch's own config
    assert ".github/workflows/ci.yml" in analyzed             # your other workflows are still checked
