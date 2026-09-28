"""Caches AI results by content hash so unchanged files are not re-sent to the models.

Every run still reports on the whole repository: cached results for unchanged
files are merged with fresh results for changed ones. Only entries used in the
current run are saved, so the cache never grows without bound.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path


class Cache:
    def __init__(self, directory: Path):
        self.path = directory / "analysis-cache.json"
        self._lock = threading.Lock()
        self._data: dict = {}
        self._used: dict = {}
        self.hits = 0
        self.misses = 0
        self._last_save = time.monotonic()
        try:
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    @staticmethod
    def key(*parts: object) -> str:
        return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()

    def get(self, key: str):
        with self._lock:
            if key in self._data:
                self.hits += 1
                self._used[key] = self._data[key]
                return self._data[key]
            self.misses += 1
            return None

    def put(self, key: str, value) -> None:
        with self._lock:
            self._used[key] = value
            if time.monotonic() - self._last_save > 30:  # keep progress even if the job is killed
                self._save_locked()

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._used), encoding="utf-8")
        tmp.replace(self.path)
        self._last_save = time.monotonic()
