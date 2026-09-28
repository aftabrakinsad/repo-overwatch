"""Logging that renders nicely in GitHub Actions (groups, warnings) and locally."""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager


class _Log:
    @property
    def _gha(self) -> bool:
        return os.environ.get("GITHUB_ACTIONS") == "true"

    def info(self, message: str) -> None:
        print(message, flush=True)

    def warning(self, message: str) -> None:
        print(f"::warning::{message}" if self._gha else f"WARNING: {message}", flush=True)

    def error(self, message: str) -> None:
        print(f"::error::{message}" if self._gha else f"ERROR: {message}", file=sys.stderr if not self._gha else sys.stdout, flush=True)

    @contextmanager
    def group(self, title: str):
        print(f"::group::{title}" if self._gha else f"\n== {title} ==", flush=True)
        try:
            yield
        finally:
            if self._gha:
                print("::endgroup::", flush=True)


log = _Log()
