"""Command-line entry point: `python -m overwatch --workspace <repo>`."""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from .config import load
from .log import log
from .pipeline import main


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="overwatch", description="AI repository watchdog")
    parser.add_argument("--workspace", default=os.environ.get("GITHUB_WORKSPACE", "."), help="repository to scan")
    parser.add_argument("--config", help="config file path relative to the workspace (default .overwatch.yml)")
    parser.add_argument("--dry-run", action="store_true", help="analyze and write the report, change nothing on GitHub")
    args = parser.parse_args(argv)
    try:
        cfg = load(Path(args.workspace), dry_run=True if args.dry_run else None, config_path=args.config)
        return main(cfg)
    except Exception as exc:  # surface crashes clearly in the Actions log
        log.error(f"Repo Overwatch crashed: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(cli())
