"""Free, deterministic checks for Markdown mistakes that break how a page renders.

These run without any AI request and report only unambiguous problems:
- an HTML tag with no name (`< width="100%" src=...>`), which GitHub shows as plain text
- a code block (``` or ~~~) that is opened but never closed, which swallows the rest of the page
- block HTML tags (div, table, details, ...) that are opened more often than closed, or vice versa
Code blocks, inline code and HTML comments are ignored, so examples in docs don't trigger findings.
"""
from __future__ import annotations

import posixpath
import re

from .models import FileInfo, Finding

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_NAMELESS_TAG = re.compile(r"<[ \t]+([A-Za-z_:][\w:.-]*)[ \t]*=[ \t]*[\"']")
_BALANCED_TAGS = ("div", "table", "details", "summary", "picture", "blockquote", "center")


def _blank(match: re.Match) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _strip_fences(text: str) -> tuple[str, int | None]:
    """Blank out fenced code blocks (keeping line numbers). Returns the text and the
    line number of an unclosed fence, if any."""
    out, open_marker, open_line = [], "", None
    for number, line in enumerate(text.split("\n"), 1):
        match = _FENCE_LINE.match(line)
        if open_marker:
            if match and match.group(1)[0] == open_marker[0] and len(match.group(1)) >= len(open_marker) \
                    and not line.strip()[len(match.group(1)):].strip():
                open_marker, open_line = "", None
            out.append("")
            continue
        if match:
            open_marker, open_line = match.group(1), number
            out.append("")
            continue
        out.append(line)
    return "\n".join(out), open_line


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _finding(f: FileInfo, line: int, title: str, explanation: str, why: str, change: str,
             rule: str, severity: str = "medium", edits: list[dict] | None = None) -> Finding:
    return Finding(
        source="markdown",
        category="broken-code",
        severity=severity,
        path=f.path,
        start_line=line,
        end_line=line,
        title=title,
        rule_id=rule,
        verified=True,
        explanation=explanation,
        why=why,
        suggested_change=change,
        edits=edits or [],
    )


def check(files: dict[str, FileInfo]) -> list[Finding]:
    findings: list[Finding] = []
    for f in files.values():
        if f.language != "markdown":
            continue
        name = posixpath.basename(f.path)
        text, unclosed_fence = _strip_fences(_COMMENT.sub(_blank, f.text))
        text = _INLINE_CODE.sub(_blank, text)
        lines = f.text.split("\n")

        for match in _NAMELESS_TAG.finditer(text):
            line = _line_of(text, match.start())
            original = lines[line - 1]
            snippet = original.strip()
            edits = []
            # If the attributes show an image (src= without href=), the fix is unambiguous.
            if re.search(r"\bsrc\s*=", original) and not re.search(r"\bhref\s*=", original) and ">" in original:
                fixed = re.sub(r"<[ \t]+(?=[A-Za-z_:][\w:.-]*[ \t]*=)", "<img ", original, count=1)
                if fixed != original and f.text.count(original) == 1:
                    edits = [{"path": f.path, "old": original, "new": fixed}]
            findings.append(_finding(
                f, line,
                f"HTML tag with no name in {name}",
                f"Line {line} starts an HTML tag without a tag name: `{snippet[:80]}{'…' if len(snippet) > 80 else ''}`. "
                "Browsers and GitHub do not treat `< ` followed by attributes as a tag.",
                "GitHub shows it as literal text or drops it, so the element (for example an image or banner) never appears.",
                "Add the missing tag name right after `<`. For an image, that is `<img width=...`."
                if edits else "Add the missing tag name right after `<` (for example `img`, `a` or `div`).",
                "markdown:nameless-html-tag", "medium", edits,
            ))

        if unclosed_fence is not None:
            findings.append(_finding(
                f, unclosed_fence,
                f"Code block opened but never closed in {name}",
                f"The code block that starts on line {unclosed_fence} has no closing fence.",
                "Everything after it renders as one big code block, hiding the rest of the page's formatting.",
                "Add a closing fence (the same ``` or ~~~ marker) where the code example ends.",
                "markdown:unclosed-code-fence",
            ))

        for tag in _BALANCED_TAGS:
            opens = [m.start() for m in re.finditer(rf"<{tag}(?=[\s>/])(?![^>]*/>)", text, re.I)]
            closes = [m.start() for m in re.finditer(rf"</{tag}\s*>", text, re.I)]
            if len(opens) == len(closes):
                continue
            more_open = len(opens) > len(closes)
            line = _line_of(text, (opens[-1] if more_open else closes[-1]))
            findings.append(_finding(
                f, line,
                f"Unbalanced <{tag}> tags in {name}",
                f"{name} opens `<{tag}>` {len(opens)} times but closes it {len(closes)} times"
                f" (last {'opening' if more_open else 'closing'} tag on line {line}).",
                "Unbalanced HTML makes the following content inherit the wrong layout or disappear, and GitHub may stop rendering Markdown inside it.",
                f"Add the missing `</{tag}>` where that section ends." if more_open
                else f"Remove the extra `</{tag}>` or add the matching opening tag.",
                f"markdown:unbalanced-{tag}", "low",
            ))
    return findings
