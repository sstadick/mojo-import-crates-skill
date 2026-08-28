#!/usr/bin/env python3
"""Format every tracked-source Mojo file in the standalone project."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    manifest = tomllib.loads((ROOT / "pixi.toml").read_text(encoding="utf-8"))
    package_path = manifest["package"]["build"]["config"]["pkg"]["path"]
    roots = [
        ROOT / package_path,
        ROOT / "tests",
        ROOT / "examples",
        ROOT / "ci" / "git-consumer",
    ]
    sources = sorted(
        path
        for source_root in roots
        if source_root.is_dir()
        for path in source_root.rglob("*.mojo")
    )
    if not sources:
        raise SystemExit("No Mojo sources found to format")
    mojo = shutil.which("mojo") or "mojo"
    subprocess.run(
        [mojo, "format", "--line-length", "100", *(str(path) for path in sources)],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
