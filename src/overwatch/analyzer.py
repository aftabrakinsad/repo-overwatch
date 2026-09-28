"""First pass: Gemini reads the whole repository and proposes candidate findings."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cache import Cache
from .config import Config
from .depgraph import DepGraph
from .llm import QuotaExhausted
from .log import log
from .models import CATEGORIES, FileInfo, Finding, norm_category, norm_severity
from .repo import is_manifest, numbered, outline

PROMPT_VERSION = "code-1"
DOCS_PROMPT_VERSION = "docs-1"

CODE_SYSTEM = f"""You are Overwatch, a meticulous senior software engineer auditing a repository.
Find real, actionable problems in the files you are given. Categories:
- bug: logic errors, wrong conditions, off-by-one, null/undefined handling, incorrect API usage
- broken-code: code that cannot compile or run, references to missing symbols/files/modules, caller and callee signatures that do not match
- error-handling: swallowed errors, unhandled promise rejections or results, crashes on recoverable errors
- security: injection, path traversal, unsafe deserialization, missing authorization, weak crypto, hardcoded credentials
- performance: needless quadratic work, expensive calls inside loops, blocking I/O on hot or async paths, unbounded memory growth
- unused-code: unused imports, variables, parameters, functions, exports or whole files
- dead-code: unreachable branches, code after return, always-true/false conditions, large commented-out code blocks
- duplication: substantial copy-pasted logic that should be shared
- concurrency: races, deadlocks, missing awaits
- type-safety: unsound casts, `any` that hides a real bug, unchecked indexing
- config: broken or inconsistent build, CI, container or tool configuration; scripts referencing missing files
- maintainability: only significant problems that are likely to cause bugs, never style preferences

Rules:
1. Only report issues you can point to in the provided code, citing exact line numbers from the numbered listing.
2. Precision over volume. No formatting, naming-taste or style nitpicks. No speculation.
3. Before calling something unused, check the linked-file map: code used by another file is not unused. If callers may exist outside what you can see, say so and use severity "low".
4. When a problem forces changes in other files (callers, implementations, tests, docs), list those paths in related_files.
5. Severity: critical = security hole, data loss, or crash on a main path; high = wrong behaviour users are likely to hit; medium = real bug in edge cases or a notable performance/maintainability risk; low = cleanup such as unused or dead code.
6. Every "path" must be one of the files in this batch. Allowed categories: {", ".join(CATEGORIES)}.

Respond with JSON only, in exactly this shape:
{{"findings": [{{"path": "src/app.ts", "start_line": 10, "end_line": 14, "category": "bug", "severity": "high", "title": "Short summary (max 12 words)", "description": "What is wrong and why, 1-4 sentences", "related_files": ["src/other.ts"]}}]}}
Return {{"findings": []}} when a batch has no real problems."""

DOCS_SYSTEM = """You are Overwatch, auditing whether a repository's documentation still matches its code.
You receive documentation files, package manifests, and an outline of the code (top-level declarations with line numbers).

Report doc-drift only where you can see a concrete mismatch, for example:
- documented functions, classes, CLI commands, flags, options, environment variables, config keys, API routes or endpoints that do not exist in the code, or exist with a different name, signature or default
- install, build, test or run instructions that reference scripts, commands, files or versions that do not match the manifests
- important public behaviour or configuration present in the code but missing from docs meant to describe it
- examples in the docs that would fail against the current code

Rules:
1. Cite the documentation file and exact line numbers as "path", "start_line", "end_line". If the fix belongs in code instead, still cite the doc lines and list the code file in related_files.
2. Always list the code or manifest files involved in related_files.
3. Do not report tone, grammar or formatting. Do not guess when the outline does not show enough.
4. Severity: high = following the docs fails (wrong commands or APIs); medium = misleading details; low = minor omissions.

Respond with JSON only:
{"findings": [{"path": "README.md", "start_line": 40, "end_line": 44, "category": "doc-drift", "severity": "medium", "title": "Short summary", "description": "What the docs say versus what the code does", "related_files": ["src/cli.ts"]}]}"""


def _file_tree(files: dict[str, FileInfo], limit: int = 3000) -> str:
    paths = sorted(files)
    tree = "\n".join(paths[:limit])
    return tree + (f"\n... ({len(paths) - limit} more files)" if len(paths) > limit else "")


def clean_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _parse(data: dict, allowed: dict[str, FileInfo], all_paths: set[str], source: str, force_category: str = "") -> list[Finding]:
    out = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        path = clean_path(item.get("path"))
        f = allowed.get(path)
        if f is None:
            continue
        try:
            start = int(item.get("start_line") or 1)
            end = int(item.get("end_line") or start)
        except (TypeError, ValueError):
            start, end = 1, 1
        start = min(max(1, start), f.line_count)
        end = min(max(start, end), f.line_count)
        related = [clean_path(r) for r in item.get("related_files") or [] if isinstance(r, str)]
        out.append(
            Finding(
                source=source,
                category=force_category or norm_category(item.get("category")),
                severity=norm_severity(item.get("severity")),
                path=path,
                start_line=start,
                end_line=end,
                title=str(item.get("title") or "Untitled finding").strip()[:140],
                description=str(item.get("description") or "").strip(),
                related_files=[r for r in related if r in all_paths and r != path][:10],
            )
        )
    return out


def _batches(items: list[tuple[FileInfo, str]], budget: int) -> list[list[tuple[FileInfo, str, str]]]:
    batches: list[list[tuple[FileInfo, str, str]]] = []
    current: list[tuple[FileInfo, str, str]] = []
    size = 0
    for f, key in items:
        body = numbered(f.text)
        if len(body) > budget:
            body = body[:budget] + "\n  ...| (file truncated for length)"
        if current and size + len(body) > budget:
            batches.append(current)
            current, size = [], 0
        current.append((f, key, body))
        size += len(body)
    if current:
        batches.append(current)
    return batches


def analyze_code(cfg: Config, files: dict[str, FileInfo], graph: DepGraph, hints: list[Finding], gemini, cache: Cache) -> tuple[list[Finding], list[str]]:
    code_files = sorted((f for f in files.values() if not f.is_doc), key=lambda f: f.path)
    results: list[Finding] = []
    todo: list[tuple[FileInfo, str]] = []
    for f in code_files:
        key = Cache.key("gemini", PROMPT_VERSION, cfg.gemini_model, f.path, f.sha)
        cached = cache.get(key)
        if cached is None:
            todo.append((f, key))
        else:
            results.extend(Finding.from_dict(d) for d in cached)
    log.info(f"Gemini code pass: {len(code_files)} files, {len(code_files) - len(todo)} from cache, {len(todo)} to analyze")
    if not todo:
        return results, []

    hints_by_path: dict[str, list[str]] = defaultdict(list)
    for h in hints:
        hints_by_path[h.path].append(f"- {h.path}:{h.start_line} [{h.rule_id}] {h.description[:200]}")
    languages = Counter(f.language for f in code_files).most_common(8)
    header = (
        f"Repository: {cfg.repo} (branch {cfg.branch})\n"
        f"Main languages: {', '.join(f'{l} ({n})' for l, n in languages)}\n\n"
        f"All analyzed files in the repository:\n{_file_tree(files)}\n"
    )
    all_paths = set(files)

    def run(batch: list[tuple[FileInfo, str, str]]) -> list[Finding]:
        link_lines, hint_lines, bodies = [], [], []
        for f, _, body in batch:
            imports = sorted(graph.imports.get(f.path, ()))[:15]
            users = sorted(graph.dependents.get(f.path, ()))[:15]
            link_lines.append(f"- {f.path}: imports {imports or '[]'}; imported by {users or '[]'}")
            hint_lines.extend(hints_by_path.get(f.path, [])[:20])
            bodies.append(f"=== FILE: {f.path} ({f.language}) ===\n{body}")
        prompt = (
            header
            + "\nLinked-file map for this batch (in-repo imports only):\n" + "\n".join(link_lines)
            + ("\n\nStatic-analysis hints (Semgrep) to confirm or dismiss:\n" + "\n".join(hint_lines) if hint_lines else "")
            + "\n\nFiles to review:\n\n" + "\n\n".join(bodies)
        )
        data = gemini.json(CODE_SYSTEM, prompt)
        batch_files = {f.path: f for f, _, _ in batch}
        found = _parse(data, batch_files, all_paths, "gemini")
        grouped: dict[str, list[dict]] = defaultdict(list)
        for finding in found:
            grouped[finding.path].append(finding.to_dict())
        for f, key, _ in batch:
            cache.put(key, grouped.get(f.path, []))  # empty lists are cached too
        return found

    errors: list[str] = []
    batches = _batches(todo, cfg.batch_chars)
    stopped, waiting = "", 0
    with ThreadPoolExecutor(max_workers=min(3, cfg.concurrency)) as pool:
        futures = {pool.submit(run, b): b for b in batches}
        for future in as_completed(futures):
            try:
                results.extend(future.result())
            except QuotaExhausted as exc:
                stopped = str(exc)
                waiting += len(futures[future])
            except Exception as exc:
                paths = ", ".join(f.path for f, _, _ in futures[future][:3])
                errors.append(f"Gemini batch starting with {paths} failed: {exc}")
                log.warning(errors[-1])
    if stopped:
        errors.append(f"Stopped early: {stopped}. {waiting} files are still waiting for the first-pass analysis.")
        log.warning(errors[-1])
    return results, errors


def analyze_docs(cfg: Config, files: dict[str, FileInfo], gemini, cache: Cache) -> tuple[list[Finding], list[str]]:
    docs = sorted((f for f in files.values() if f.is_doc), key=lambda f: f.path)
    if not docs:
        return [], []
    code = sorted((f for f in files.values() if not f.is_doc), key=lambda f: f.path)
    outline_parts, size = [], 0
    for f in code:
        lines = outline(f)
        if not lines:
            continue
        block = f"--- {f.path}\n" + "\n".join(lines)
        if size + len(block) > 150_000:
            outline_parts.append("... (outline truncated)")
            break
        outline_parts.append(block)
        size += len(block)
    outline_text = "\n".join(outline_parts)
    manifests = "\n\n".join(
        f"=== MANIFEST: {f.path} ===\n{f.text[:8000]}" for f in code if is_manifest(f.path)
    )[:60_000]
    context_hash = Cache.key(outline_text, manifests)
    budget = max(60_000, cfg.batch_chars - len(outline_text) - len(manifests))

    results: list[Finding] = []
    todo: list[tuple[FileInfo, str]] = []
    for f in docs:
        key = Cache.key("gemini-docs", DOCS_PROMPT_VERSION, cfg.gemini_model, f.path, f.sha, context_hash)
        cached = cache.get(key)
        if cached is None:
            todo.append((f, key))
        else:
            results.extend(Finding.from_dict(d) for d in cached)
    log.info(f"Gemini docs pass: {len(docs)} docs, {len(docs) - len(todo)} from cache, {len(todo)} to analyze")
    all_paths = set(files)

    errors: list[str] = []
    batches = _batches(todo, budget)
    for index, batch in enumerate(batches):
        prompt = (
            f"Repository: {cfg.repo}\n\nCode outline:\n{outline_text}\n\n{manifests}\n\nDocumentation files:\n\n"
            + "\n\n".join(f"=== DOC: {f.path} ===\n{body}" for f, _, body in batch)
        )
        try:
            data = gemini.json(DOCS_SYSTEM, prompt)
        except QuotaExhausted as exc:
            waiting = sum(len(b) for b in batches[index:])
            errors.append(f"Stopped early: {exc}. {waiting} documentation files are still waiting for the docs check.")
            log.warning(errors[-1])
            break
        except Exception as exc:
            errors.append(f"Gemini docs batch failed: {exc}")
            log.warning(errors[-1])
            continue
        batch_files = {f.path: f for f, _, _ in batch}
        found = _parse(data, batch_files, all_paths, "docs", force_category="doc-drift")
        grouped: dict[str, list[dict]] = defaultdict(list)
        for finding in found:
            grouped[finding.path].append(finding.to_dict())
        for f, key, _ in batch:
            cache.put(key, grouped.get(f.path, []))
        results.extend(found)
    return results, errors


def dumps(findings: list[Finding]) -> str:
    return json.dumps([f.to_dict() for f in findings], indent=2)
