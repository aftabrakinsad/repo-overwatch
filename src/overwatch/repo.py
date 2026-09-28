"""Finds the files to analyze, detects their language and builds code outlines."""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
from collections import Counter
from pathlib import Path

import pathspec

from .config import Config, git
from .models import FileInfo

# Never analyzed by the AI models: generated/vendored code, lockfiles (OSV-Scanner
# reads those), binaries, and anything that is likely to hold real secrets.
DEFAULT_EXCLUDES = [
    ".git/", "node_modules/", "bower_components/", "vendor/", "third_party/", "third-party/",
    "dist/", "build/", "out/", "target/", ".next/", ".nuxt/", ".svelte-kit/", ".output/",
    ".venv/", "venv/", "env/", "__pycache__/", ".mypy_cache/", ".pytest_cache/", ".tox/",
    "coverage/", ".coverage", "htmlcov/", ".idea/", ".vscode/", ".gradle/", "Pods/",
    "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "pnpm-lock.yaml",
    "npm-shrinkwrap.json", "go.sum", "bun.lockb",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.icns", "*.bmp", "*.tif", "*.tiff",
    "*.psd", "*.pdf", "*.zip", "*.gz", "*.tgz", "*.tar", "*.7z", "*.rar", "*.jar", "*.war",
    "*.class", "*.exe", "*.dll", "*.so", "*.dylib", "*.a", "*.o", "*.obj", "*.wasm", "*.bin",
    "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot", "*.mp3", "*.mp4", "*.mov", "*.wav",
    "*.ogg", "*.webm", "*.snap", "*.lcov", "*.pyc",
    ".env", ".env.*", "!.env.example", "!.env.sample", "!.env.template",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "id_rsa*", "id_ed25519*",
]

LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".mts": "typescript", ".cts": "typescript", ".svelte": "svelte", ".vue": "vue",
    ".astro": "astro", ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".kts": "kotlin", ".scala": "scala", ".groovy": "groovy", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".m": "objective-c", ".mm": "objective-c", ".cs": "csharp", ".fs": "fsharp",
    ".swift": "swift", ".dart": "dart", ".rb": "ruby", ".php": "php", ".pl": "perl",
    ".lua": "lua", ".r": "r", ".jl": "julia", ".ex": "elixir", ".exs": "elixir",
    ".erl": "erlang", ".hs": "haskell", ".clj": "clojure", ".zig": "zig", ".nim": "nim",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell",
    ".bat": "batch", ".cmd": "batch", ".sql": "sql", ".graphql": "graphql", ".gql": "graphql",
    ".proto": "protobuf", ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".sass": "sass", ".less": "less", ".json": "json", ".jsonc": "json", ".json5": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".conf": "config", ".xml": "xml", ".gradle": "gradle", ".tf": "terraform",
    ".hcl": "hcl", ".nix": "nix", ".cmake": "cmake",
    ".md": "markdown", ".mdx": "markdown", ".rst": "restructuredtext", ".adoc": "asciidoc",
    ".txt": "text",
}

LANG_BY_NAME = {
    "dockerfile": "dockerfile", "containerfile": "dockerfile", "makefile": "make",
    "gnumakefile": "make", "justfile": "just", "jenkinsfile": "groovy", "gemfile": "ruby",
    "rakefile": "ruby", "vagrantfile": "ruby", "procfile": "config",
    "cmakelists.txt": "cmake", "requirements.txt": "pip-requirements", "go.mod": "go-mod",
    ".env.example": "dotenv", ".env.sample": "dotenv", ".env.template": "dotenv",
    ".editorconfig": "ini", ".gitattributes": "config",
}

DOC_LANGS = {"markdown", "restructuredtext", "asciidoc"}
DOC_DIRS = ("docs/", "doc/", "documentation/", "wiki/")

MANIFEST_NAMES = {
    "package.json", "cargo.toml", "pyproject.toml", "setup.cfg", "setup.py", "go.mod",
    "composer.json", "gemfile", "pom.xml", "build.gradle", "build.gradle.kts", "makefile",
    "justfile", "tauri.conf.json", "deno.json", "dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "compose.yml", "compose.yaml", ".env.example",
}


def detect_language(path: str) -> str:
    name = posixpath.basename(path).lower()
    if name in LANG_BY_NAME:
        return LANG_BY_NAME[name]
    if name.startswith("dockerfile."):
        return "dockerfile"
    ext = posixpath.splitext(name)[1]
    lang = LANG_BY_EXT.get(ext, "")
    if lang == "text" and not path.lower().startswith(DOC_DIRS):
        return ""  # stray .txt files outside docs are usually data, not documentation
    return lang


def list_paths(workspace: Path) -> list[str]:
    out = git(workspace, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if out:
        return sorted({p for p in out.split("\0") if p})
    paths = []
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in names:
            paths.append(Path(root, name).relative_to(workspace).as_posix())
    return sorted(paths)


def _spec(patterns: list[str]) -> pathspec.PathSpec | None:
    return pathspec.GitIgnoreSpec.from_lines(patterns) if patterns else None


def collect(cfg: Config) -> tuple[dict[str, FileInfo], list[str], Counter]:
    """Returns (analyzable files, every path in the repo, counts of skipped files by reason)."""
    all_paths = list_paths(cfg.workspace)
    exclude = _spec(DEFAULT_EXCLUDES + cfg.exclude)
    include = _spec(cfg.include)
    docs = _spec(cfg.docs)

    files: dict[str, FileInfo] = {}
    skipped: Counter = Counter()
    for index, path in enumerate(all_paths):
        if len(files) >= cfg.max_files:
            skipped["over the max_files limit"] += len(all_paths) - index
            break
        if exclude and exclude.match_file(path):
            skipped["excluded"] += 1
            continue
        if include and not include.match_file(path):
            skipped["not in include list"] += 1
            continue
        language = detect_language(path)
        is_doc = language in DOC_LANGS or language == "text" or bool(docs and docs.match_file(path))
        if not language:
            skipped["unsupported file type"] += 1
            continue
        full = cfg.workspace / path
        if full.is_symlink() or not full.is_file():
            skipped["symlink or missing"] += 1
            continue
        if full.stat().st_size > cfg.max_file_bytes:
            skipped["larger than max_file_bytes"] += 1
            continue
        data = full.read_bytes()
        if b"\x00" in data[:8192]:
            skipped["binary"] += 1
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped["not UTF-8"] += 1
            continue
        if not text.strip():
            skipped["empty"] += 1
            continue
        files[path] = FileInfo(
            path=path,
            text=text,
            sha=hashlib.sha256(data).hexdigest(),
            language=language,
            is_doc=is_doc,
        )
    return files, all_paths, skipped


def numbered(text: str, start: int = 1) -> str:
    return "\n".join(f"{i:>5}| {line}" for i, line in enumerate(text.splitlines(), start))


def excerpt(text: str, ranges: list[tuple[int, int]], limit: int = 120_000, pad: int = 120) -> str:
    """The whole file when it is small enough, otherwise windows around the given ranges."""
    if len(text) <= limit:
        return numbered(text)
    lines = text.splitlines()
    windows: list[list[int]] = []
    for start, end in sorted(ranges):
        lo, hi = max(1, start - pad), min(len(lines), max(end, start) + pad)
        if windows and lo <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])
    parts, cursor = [], 1
    for lo, hi in windows:
        if lo > cursor:
            parts.append(f"  ...| (lines {cursor}-{lo - 1} omitted)")
        parts.append(numbered("\n".join(lines[lo - 1 : hi]), lo))
        cursor = hi + 1
    if cursor <= len(lines):
        parts.append(f"  ...| (lines {cursor}-{len(lines)} omitted)")
    return "\n".join(parts)


_SIGNATURE = re.compile(
    r"^(?:export\s+|pub(?:\([^)]*\))?\s+|public\s+|internal\s+|protected\s+|private\s+|"
    r"static\s+|async\s+|abstract\s+|final\s+|default\s+|override\s+|open\s+|data\s+|"
    r"sealed\s+|unsafe\s+|extern\s+)*"
    r"(?:def|class|function|fn|func|interface|type|struct|enum|trait|impl|const|let|var|val|"
    r"object|record|module|namespace|macro_rules!)\b"
)


def outline(file: FileInfo, max_lines: int = 80) -> list[str]:
    """Top-level declarations with line numbers: a cheap, language-agnostic API summary."""
    out: list[str] = []
    for number, line in enumerate(file.text.splitlines(), 1):
        stripped = line.lstrip()
        if len(line) - len(stripped) <= 4 and _SIGNATURE.match(stripped):
            out.append(f"{number:>5}| {line.rstrip()[:160]}")
            if len(out) >= max_lines:
                out.append("  ...| (outline truncated)")
                break
    return out


def is_manifest(path: str) -> bool:
    return posixpath.basename(path).lower() in MANIFEST_NAMES
