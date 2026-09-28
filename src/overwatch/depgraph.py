"""Builds a file-level dependency graph so every finding knows its linked files.

The resolvers use lightweight import patterns for the most common languages
rather than full parsers. They favour precision: an import that cannot be
resolved to a file in the repository is ignored (third-party packages, for
example), so the graph only contains real in-repo links.
"""
from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from urllib.parse import unquote

from .models import FileInfo, Finding

JS_LANGS = {"javascript", "typescript", "svelte", "vue", "astro"}
JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".svelte", ".vue", ".json", ".d.ts"]
CSS_LANGS = {"css", "scss", "sass", "less"}


class PathIndex:
    def __init__(self, paths: list[str]):
        self.paths = set(paths)
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.by_dir: dict[str, list[str]] = defaultdict(list)
        self.dirs: set[str] = set()
        for p in paths:
            self.by_name[posixpath.basename(p)].append(p)
            d = posixpath.dirname(p)
            self.by_dir[d].append(p)
            while d:
                self.dirs.add(d)
                d = posixpath.dirname(d)

    def exists(self, path: str) -> bool:
        return path in self.paths

    def is_dir(self, path: str) -> bool:
        return path.rstrip("/") in self.dirs or path in ("", ".")

    def suffix(self, suffix: str, limit: int = 3) -> list[str]:
        suffix = suffix.lstrip("/")
        hits = [p for p in self.by_name.get(posixpath.basename(suffix), []) if p == suffix or p.endswith("/" + suffix)]
        return hits if len(hits) <= limit else []  # too ambiguous to trust

    def first(self, candidates: list[str]) -> str | None:
        for c in candidates:
            if c in self.paths:
                return c
        return None


class DepGraph:
    def __init__(self) -> None:
        self.imports: dict[str, set[str]] = defaultdict(set)
        self.dependents: dict[str, set[str]] = defaultdict(set)

    def add(self, src: str, dst: str) -> None:
        if src != dst:
            self.imports[src].add(dst)
            self.dependents[dst].add(src)

    def linked(self, path: str, limit: int = 20) -> list[str]:
        seen: list[str] = []
        for p in sorted(self.imports.get(path, ())) + sorted(self.dependents.get(path, ())):
            if p not in seen:
                seen.append(p)
        return seen[:limit]


def _norm(path: str) -> str:
    normalized = posixpath.normpath(path) if path else "."
    return "" if normalized == "." else normalized.lstrip("/")


def _rel(from_path: str, spec: str) -> str | None:
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec))
    return None if joined.startswith("..") else joined


# ---------------------------------------------------------------- JS / TS
_JS_PATTERNS = [
    re.compile(r"""\b(?:import|export)\s+(?:type\s+)?[^'"`;]*?\bfrom\s*['"]([^'"\n]+)['"]"""),
    re.compile(r"""\bimport\s*['"]([^'"\n]+)['"]"""),
    re.compile(r"""\b(?:require|import)\s*\(\s*['"]([^'"\n]+)['"]\s*\)"""),
]


def _js_candidates(base: str) -> list[str]:
    stem, ext = posixpath.splitext(base)
    cands = [base] + [base + e for e in JS_EXTS] + [f"{base}/index{e}" for e in JS_EXTS]
    if ext in (".js", ".jsx", ".mjs", ".cjs"):  # TypeScript ESM imports use .js for .ts files
        cands += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
    return cands


def _resolve_js(spec: str, src: str, idx: PathIndex) -> str | None:
    if spec.startswith("."):
        base = _rel(src, spec)
        return idx.first(_js_candidates(base)) if base else None
    for alias, targets in (("$lib/", ["src/lib/"]), ("@/", ["src/", ""]), ("~/", ["src/", ""])):
        if spec.startswith(alias):
            rest = spec[len(alias):]
            for t in targets:
                hit = idx.first(_js_candidates(t + rest))
                if hit:
                    return hit
            for cand in _js_candidates(targets[0] + rest):  # monorepo: package lives in a subfolder
                hits = idx.suffix(cand, limit=1)
                if hits:
                    return hits[0]
    return None


# ---------------------------------------------------------------- Python
_PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.+[\w.]*|[\w.]+)[ \t]+import[ \t]+\(?([^\n#]+)", re.M)
_PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]*,[ \t]*[\w.]+)*)", re.M)


def _py_module(mod_path: str, idx: PathIndex, absolute: bool) -> list[str]:
    if not mod_path:
        return []
    cands = [mod_path + ".py", mod_path + "/__init__.py"]
    if not absolute:
        hit = idx.first(cands)
        return [hit] if hit else []
    for c in cands:
        hits = idx.suffix(c, limit=1)
        if hits:
            return hits
    return []


def _resolve_python(text: str, src: str, idx: PathIndex) -> set[str]:
    out: set[str] = set()
    for match in _PY_FROM.finditer(text):
        module, names = match.group(1), match.group(2)
        imported = [n.strip().split(" as ")[0].strip(" ()") for n in names.split(",")]
        if module.startswith("."):
            level = len(module) - len(module.lstrip("."))
            base = posixpath.dirname(src)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            rest = module.lstrip(".").replace(".", "/")
            target = posixpath.join(base, rest) if rest else base
            out.update(_py_module(target, idx, absolute=False))
            for name in imported:
                if name and name != "*":
                    out.update(_py_module(posixpath.join(target, name), idx, absolute=False))
        else:
            target = module.replace(".", "/")
            found = _py_module(target, idx, absolute=True)
            out.update(found)
            for name in imported:
                if name and name != "*":
                    out.update(_py_module(f"{target}/{name}", idx, absolute=True))
    for match in _PY_IMPORT.finditer(text):
        for module in match.group(1).split(","):
            out.update(_py_module(module.strip().replace(".", "/"), idx, absolute=True))
    return out


# ---------------------------------------------------------------- Rust
_RS_MOD = re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?mod[ \t]+(\w+)[ \t]*;", re.M)
_RS_USE = re.compile(r"\buse[ \t]+crate::([\w:]+)")


def _resolve_rust(text: str, src: str, idx: PathIndex) -> set[str]:
    out: set[str] = set()
    directory = posixpath.dirname(src)
    stem = posixpath.splitext(posixpath.basename(src))[0]
    mod_dir = directory if stem in ("mod", "lib", "main") else posixpath.join(directory, stem)
    for name in _RS_MOD.findall(text):
        hit = idx.first([posixpath.join(mod_dir, f"{name}.rs"), posixpath.join(mod_dir, name, "mod.rs")])
        if hit:
            out.add(hit)
    crate_src = None
    d = directory
    while True:
        if idx.exists(posixpath.join(d, "Cargo.toml") if d else "Cargo.toml"):
            crate_src = posixpath.join(d, "src") if d else "src"
            break
        if not d:
            break
        d = posixpath.dirname(d)
    if crate_src:
        for path in _RS_USE.findall(text):
            parts = [p for p in path.split("::") if p]
            for i in range(len(parts), 0, -1):
                base = posixpath.join(crate_src, *parts[:i])
                hit = idx.first([base + ".rs", posixpath.join(base, "mod.rs")])
                if hit:
                    out.add(hit)
                    break
    return out


# ---------------------------------------------------------------- Go
_GO_BLOCK = re.compile(r"\bimport\s*\(([^)]*)\)", re.S)
_GO_SINGLE = re.compile(r'\bimport\s+(?:[\w.]+\s+)?"([^"]+)"')


def _resolve_go(text: str, idx: PathIndex, modules: list[tuple[str, str]]) -> set[str]:
    imports = set(_GO_SINGLE.findall(text))
    for block in _GO_BLOCK.findall(text):
        imports.update(re.findall(r'"([^"]+)"', block))
    out: set[str] = set()
    for imp in imports:
        for mod_dir, mod_path in modules:
            if imp == mod_path or imp.startswith(mod_path + "/"):
                target = posixpath.join(mod_dir, imp[len(mod_path):].lstrip("/")).rstrip("/")
                target = _norm(target)
                out.update(p for p in idx.by_dir.get(target, []) if p.endswith(".go") and not p.endswith("_test.go"))
    return out


# ---------------------------------------------------------------- others
_C_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"', re.M)
_JVM_IMPORT = re.compile(r"^[ \t]*import[ \t]+(?:static[ \t]+)?([\w.]+)", re.M)
_RB_REQ = re.compile(r"""\brequire_relative\s*\(?\s*['"]([^'"]+)['"]""")
_PHP_REQ = re.compile(r"""\b(?:require|include)(?:_once)?\s*\(?\s*(?:__DIR__\s*\.\s*)?['"]([^'"]+\.php)['"]""")
_DART_IMP = re.compile(r"""^\s*(?:import|export|part)\s+['"]([^'"]+\.dart)['"]""", re.M)
_CSS_IMP = re.compile(r"""@(?:import|use|forward)\s+(?:url\()?['"]([^'"]+)['"]""")
_HTML_REF = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"'#?]+)["']""")
_SCHEME = re.compile(r"^(?:[a-zA-Z][\w+.-]*:|//)")


def _resolve_misc(f: FileInfo, idx: PathIndex) -> set[str]:
    out: set[str] = set()
    lang, text, src = f.language, f.text, f.path
    if lang in ("c", "cpp", "objective-c"):
        for inc in _C_INCLUDE.findall(text):
            rel = _rel(src, inc)
            hit = idx.first([rel]) if rel else None
            out.update([hit] if hit else idx.suffix(inc))
    elif lang in ("java", "kotlin", "scala", "groovy"):
        for imp in _JVM_IMPORT.findall(text):
            base = imp.replace(".", "/")
            for ext in (".java", ".kt", ".scala", ".groovy"):
                hits = idx.suffix(base + ext, limit=1)
                if hits:
                    out.update(hits)
                    break
    elif lang == "ruby":
        for req in _RB_REQ.findall(text):
            rel = _rel(src, req if req.endswith(".rb") else req + ".rb")
            if rel and idx.exists(rel):
                out.add(rel)
    elif lang == "php":
        for req in _PHP_REQ.findall(text):
            rel = _rel(src, req.lstrip("/"))
            if rel and idx.exists(rel):
                out.add(rel)
    elif lang == "dart":
        for imp in _DART_IMP.findall(text):
            if not _SCHEME.match(imp):
                rel = _rel(src, imp)
                if rel and idx.exists(rel):
                    out.add(rel)
    elif lang in CSS_LANGS:
        for imp in _CSS_IMP.findall(text):
            if _SCHEME.match(imp):
                continue
            rel = _rel(src, imp)
            if not rel:
                continue
            d, name = posixpath.split(rel)
            cands = [rel] + [rel + e for e in (".css", ".scss", ".sass", ".less")]
            cands += [posixpath.join(d, "_" + name + e) for e in (".scss", ".sass")]
            hit = idx.first(cands)
            if hit:
                out.add(hit)
    elif lang == "html":
        for ref in _HTML_REF.findall(text):
            if _SCHEME.match(ref):
                continue
            if ref.startswith("/"):
                hit = idx.first([ref.lstrip("/"), "public" + ref, "static" + ref])
            else:
                rel = _rel(src, ref)
                hit = idx.first([rel]) if rel else None
            if hit:
                out.add(hit)
    return out


# ---------------------------------------------------------------- docs
_FENCE = re.compile(r"(^|\n)(```|~~~).*?(\n\2[^\n]*|\Z)", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_MD_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_MD_REF = re.compile(r"^[ \t]*\[[^\]\n]+\]:[ \t]*<?(\S+?)>?(?:[ \t]+.*)?$", re.M)
_CODE_PATH = re.compile(r"`((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,6})`")


def _blank(match: re.Match) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _doc_links(text: str) -> list[tuple[str, int]]:
    """Relative link targets with their line numbers, ignoring code blocks and inline code."""
    clean = _INLINE_CODE.sub(_blank, _FENCE.sub(_blank, text))
    links = []
    for pattern in (_MD_LINK, _MD_REF):
        for m in pattern.finditer(clean):
            links.append((m.group(1), clean.count("\n", 0, m.start()) + 1))
    return links


def _link_target(doc: str, raw: str) -> str | None:
    target = unquote(raw.split("#")[0].split("?")[0]).strip()
    if not target or _SCHEME.match(target) or target[0] in "{$<%":
        return None
    if target.startswith("/"):
        return _norm(target.lstrip("/"))
    rel = _rel(doc, target)
    return None if rel is None else _norm(rel)


def broken_links(files: dict[str, FileInfo], all_paths: list[str]) -> list[Finding]:
    idx = PathIndex(all_paths)
    findings = []
    for f in files.values():
        if f.language != "markdown":
            continue
        for raw, line in _doc_links(f.text):
            target = _link_target(f.path, raw)
            if target is None or idx.exists(target) or idx.is_dir(target):
                continue
            findings.append(
                Finding(
                    source="links",
                    category="doc-drift",
                    severity="low",
                    path=f.path,
                    start_line=line,
                    end_line=line,
                    title=f"Broken link to `{raw}` in {posixpath.basename(f.path)}",
                    rule_id="broken-link",
                    verified=True,
                    explanation=f"The link `{raw}` points to `{target}`, which does not exist in the repository.",
                    why="Readers following the documentation hit a 404, which usually means a file was moved, renamed or deleted without updating the docs.",
                    suggested_change="Point the link at the file's new location, or remove the link if the target is gone for good.",
                )
            )
    return findings


# ---------------------------------------------------------------- build
def build(files: dict[str, FileInfo], all_paths: list[str]) -> DepGraph:
    idx = PathIndex(all_paths)
    graph = DepGraph()
    go_modules: list[tuple[str, str]] = []
    for f in files.values():
        if posixpath.basename(f.path) == "go.mod":
            m = re.search(r"^module\s+(\S+)", f.text, re.M)
            if m:
                go_modules.append((posixpath.dirname(f.path), m.group(1)))

    for f in files.values():
        targets: set[str] = set()
        if f.language in JS_LANGS:
            for pattern in _JS_PATTERNS:
                for spec in pattern.findall(f.text):
                    hit = _resolve_js(spec, f.path, idx)
                    if hit:
                        targets.add(hit)
        elif f.language == "python":
            targets = _resolve_python(f.text, f.path, idx)
        elif f.language == "rust":
            targets = _resolve_rust(f.text, f.path, idx)
        elif f.language == "go":
            targets = _resolve_go(f.text, idx, go_modules)
        elif f.language == "markdown":
            for raw, _ in _doc_links(f.text):
                target = _link_target(f.path, raw)
                if target and idx.exists(target):
                    targets.add(target)
            for mention in _CODE_PATH.findall(f.text):
                hit = idx.first([_norm(mention)]) or next(iter(idx.suffix(mention, limit=1)), None)
                if hit:
                    targets.add(hit)
        else:
            targets = _resolve_misc(f, idx)
        for t in targets:
            if t in files:
                graph.add(f.path, t)
    return graph
