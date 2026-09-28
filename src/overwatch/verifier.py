"""Second pass: a verifier model (Claude, or Gemini in a strict second pass) checks every
candidate finding and drafts exact fixes.

Several files are verified per request to save requests on rate-limited free tiers.
Results are cached per file, so a run that stops early (quota) resumes next time.
"""
from __future__ import annotations

import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .analyzer import clean_path
from .cache import Cache
from .config import Config
from .depgraph import DepGraph
from .llm import QuotaExhausted
from .log import log
from .models import FileInfo, Finding, norm_category, norm_severity
from .repo import excerpt, numbered

PROMPT_VERSION = "verify-2"
CHUNK_SIZE = 20            # candidates per file per unit
MAX_CANDIDATES_PER_REQUEST = 40
MAX_LINKED_FILES = 5
LINKED_FILE_CHARS = 12_000

SYSTEM = """You are the verification stage of Overwatch, a repository watchdog. A first-pass reviewer (an AI model or a static analyzer) flagged candidate problems. Many first-pass findings are false positives. You are the rigorous, skeptical second reviewer: when in doubt, reject.

For each candidate (each one names the file it belongs to):
1. Decide "confirmed" or "rejected" by reading the actual code, including the linked files provided. Reject anything speculative, stylistic, already handled elsewhere, or not supported by the code you can see. If two candidates describe the same problem, confirm one and reject the other as a duplicate.
2. For confirmed findings, write for a developer who has not seen the code:
   - explanation: what exactly is wrong, referencing the code (2-5 sentences)
   - why_it_matters: the concrete consequence (bug, crash, security risk, cost, confusion)
   - suggested_change: what to change, in words, specific enough to act on
   - start_line/end_line: the precise lines of the problem in the candidate's file
   - severity: critical | high | medium | low (you may correct the first-pass severity)
   - category: keep or correct it
3. edits: a fix as exact search-and-replace operations, ONLY when you are confident the fix is correct, complete and safe.
   - "old" must be copied verbatim from the file content WITHOUT the line-number prefix ("   12| "), including indentation, and must be long enough to match exactly one place in that file.
   - "new" is the replacement text. Keep the change minimal; do not reformat unrelated code.
   - You may edit any file shown to you (files under review and linked files). Never edit other files.
   - If a correct fix needs changes you cannot see or make safely, return "edits": [] and explain in suggested_change.
4. related_updates: every other file that must change because of this problem or its fix (callers, implementers, tests, docs, config), each with a one-sentence reason.

Respond with JSON only:
{"results": [{"id": "F1", "verdict": "confirmed", "reason": "why confirmed or rejected, one sentence", "severity": "high", "category": "bug", "title": "Short precise title", "start_line": 10, "end_line": 12, "explanation": "...", "why_it_matters": "...", "suggested_change": "...", "edits": [{"path": "src/a.ts", "old": "exact text", "new": "replacement"}], "related_updates": [{"path": "src/b.ts", "reason": "..."}]}]}
Return exactly one result for every candidate id."""


@dataclass
class Unit:
    file: FileInfo
    candidates: list[Finding]
    linked: list[FileInfo]
    key: str
    size: int = 0
    ids: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> set[str]:
        return {self.file.path} | {lf.path for lf in self.linked}


def _linked_files(f: FileInfo, candidates: list[Finding], graph: DepGraph, files: dict[str, FileInfo]) -> list[FileInfo]:
    ordered: list[str] = []
    for c in candidates:
        ordered += c.related_files
    ordered += graph.linked(f.path)
    chosen: list[FileInfo] = []
    for p in ordered:
        if p != f.path and p in files and files[p] not in chosen:
            chosen.append(files[p])
        if len(chosen) >= MAX_LINKED_FILES:
            break
    return chosen


def _linked_body(lf: FileInfo) -> str:
    body = numbered(lf.text)
    return body if len(body) <= LINKED_FILE_CHARS else body[:LINKED_FILE_CHARS] + "\n  ...| (truncated)"


def _prompt(cfg: Config, batch: list[Unit], ids: dict[str, tuple[Finding, Unit]], graph: DepGraph) -> str:
    parts = [f"Repository: {cfg.repo} (branch {cfg.branch})", ""]
    reviewed: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for unit in batch:
        reviewed[unit.file.path] += [(c.start_line, c.end_line) for c in unit.candidates]
    files = {u.file.path: u.file for u in batch}
    for path, ranges in reviewed.items():
        f = files[path]
        parts += [
            f"=== FILE UNDER REVIEW: {path} ({f.language}) ===",
            f"Imports: {sorted(graph.imports.get(path, ())) or '[]'} | Imported by: {sorted(graph.dependents.get(path, ())) or '[]'}",
            excerpt(f.text, ranges),
            "",
        ]
    shown: set[str] = set(reviewed)
    for unit in batch:
        for lf in unit.linked:
            if lf.path not in shown:
                shown.add(lf.path)
                parts += [f"=== LINKED FILE: {lf.path} ({lf.language}) ===", _linked_body(lf), ""]
    candidates = [
        {
            "id": cid,
            "path": c.path,
            "source": c.source + (f" ({c.rule_id})" if c.rule_id else ""),
            "category": c.category,
            "severity": c.severity,
            "lines": f"{c.start_line}-{c.end_line}",
            "title": c.title,
            "description": c.description,
            "related_files": c.related_files,
        }
        for cid, (c, _) in ids.items()
    ]
    parts += ["Candidate findings:", json.dumps(candidates, indent=2)]
    return "\n".join(parts)


def _clean_edits(raw: object, allowed: set[str]) -> list[dict]:
    edits = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        path, old, new = clean_path(e.get("path")), e.get("old"), e.get("new", "")
        if path in allowed and isinstance(old, str) and old and isinstance(new, str) and old != new:
            edits.append({"path": path, "old": old, "new": new})
    return edits


def _apply_result(c: Finding, r: dict, f: FileInfo, allowed: set[str], all_paths: set[str]) -> Finding:
    v = Finding.from_dict(c.to_dict())
    v.verified = True
    v.severity = norm_severity(r.get("severity"), c.severity)
    v.category = norm_category(r.get("category"), c.category)
    v.title = str(r.get("title") or c.title).strip()[:140]
    try:
        start = int(r.get("start_line") or c.start_line)
        end = int(r.get("end_line") or start)
        v.start_line = min(max(1, start), f.line_count)
        v.end_line = min(max(v.start_line, end), f.line_count)
    except (TypeError, ValueError):
        pass
    v.explanation = str(r.get("explanation") or c.description).strip()
    v.why = str(r.get("why_it_matters") or "").strip()
    v.suggested_change = str(r.get("suggested_change") or "").strip()
    v.edits = _clean_edits(r.get("edits"), allowed)
    updates = []
    for u in r.get("related_updates") or []:
        if isinstance(u, dict):
            p = clean_path(u.get("path"))
            if p and p != f.path and p in all_paths:
                updates.append({"path": p, "reason": str(u.get("reason") or "").strip()})
    v.related_updates = updates[:12]
    return v


def _pack(units: list[Unit], budget: int) -> list[list[Unit]]:
    batches: list[list[Unit]] = []
    current: list[Unit] = []
    size = count = 0
    for unit in units:
        n = len(unit.candidates)
        if current and (size + unit.size > budget or count + n > MAX_CANDIDATES_PER_REQUEST):
            batches.append(current)
            current, size, count = [], 0, 0
        current.append(unit)
        size += unit.size
        count += n
    if current:
        batches.append(current)
    return batches


def verify(cfg: Config, files: dict[str, FileInfo], graph: DepGraph, candidates: list[Finding], client, model_name: str, cache: Cache, all_paths: list[str]) -> tuple[list[Finding], list[str], dict]:
    by_path: dict[str, list[Finding]] = defaultdict(list)
    for c in candidates:
        if c.path in files:
            by_path[c.path].append(c)

    verified: list[Finding] = []
    units: list[Unit] = []
    for path, group in sorted(by_path.items()):
        group.sort(key=lambda c: (c.start_line, c.title))
        f = files[path]
        for i in range(0, len(group), CHUNK_SIZE):
            chunk = group[i : i + CHUNK_SIZE]
            linked = _linked_files(f, chunk, graph, files)
            signature = json.dumps([[c.source, c.rule_id, c.category, c.start_line, c.end_line, c.title] for c in chunk])
            key = Cache.key("verify", PROMPT_VERSION, model_name, f.path, f.sha,
                            *[f"{lf.path}:{lf.sha}" for lf in linked], signature)
            cached = cache.get(key)
            if cached is not None:
                verified.extend(Finding.from_dict(d) for d in cached)
                continue
            size = len(excerpt(f.text, [(c.start_line, c.end_line) for c in chunk])) + sum(
                min(len(lf.text) * 1.1, LINKED_FILE_CHARS) for lf in linked
            )
            units.append(Unit(f, chunk, linked, key, int(size)))

    path_set = set(all_paths)
    stats = {"candidates": len(candidates), "confirmed": 0, "rejected": 0, "pending": 0}
    batches = _pack(units, cfg.batch_chars)
    log.info(f"Verification with {model_name}: {sum(len(u.candidates) for u in units)} new candidates "
             f"in {len(batches)} requests ({len(candidates) - sum(len(u.candidates) for u in units)} from cache)")

    def run(batch: list[Unit]) -> list[Finding]:
        ids: dict[str, tuple[Finding, Unit]] = {}
        for unit in batch:
            unit.ids = []
            for c in unit.candidates:
                cid = f"F{len(ids) + 1}"
                ids[cid] = (c, unit)
                unit.ids.append(cid)
        data = client.json(SYSTEM, _prompt(cfg, batch, ids, graph))
        answered: set[str] = set()
        confirmed: dict[str, list[Finding]] = defaultdict(list)
        for r in data.get("results") or []:
            if not isinstance(r, dict):
                continue
            cid = str(r.get("id", "")).strip()
            if cid not in ids:
                continue
            answered.add(cid)
            c, unit = ids[cid]
            if str(r.get("verdict", "")).lower() == "confirmed":
                confirmed[unit.key].append(_apply_result(c, r, unit.file, unit.allowed, path_set))
        out: list[Finding] = []
        for unit in batch:
            out.extend(confirmed[unit.key])
            if all(cid in answered for cid in unit.ids):  # only cache complete answers
                cache.put(unit.key, [v.to_dict() for v in confirmed[unit.key]])
        return out

    errors: list[str] = []
    stopped = ""
    pending = 0
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = {pool.submit(run, b): b for b in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                verified.extend(future.result())
            except QuotaExhausted as exc:
                stopped = str(exc)
                pending += sum(len(u.candidates) for u in batch)
            except Exception as exc:
                pending += sum(len(u.candidates) for u in batch)
                errors.append(f"Verification of {', '.join(u.file.path for u in batch[:3])} failed: {exc}")
                log.warning(errors[-1])
    if stopped:
        errors.append(f"Stopped early: {stopped}. {pending} candidates are still waiting for verification.")
        log.warning(errors[-1])
    stats["confirmed"] = len(verified)
    stats["pending"] = pending
    stats["rejected"] = max(0, len(candidates) - len(verified) - pending)
    return verified, errors, stats
