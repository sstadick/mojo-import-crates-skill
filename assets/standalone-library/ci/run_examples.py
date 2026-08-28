#!/usr/bin/env python3
"""Execute standalone Mojo examples in isolated temporary directories."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    examples = sorted((ROOT / "examples").glob("*.mojo"))
    if not examples:
        raise SystemExit("No standalone Mojo examples found below examples/")
    pixi = shutil.which("pixi") or "pixi"
    for example in examples:
        with tempfile.TemporaryDirectory(
            prefix=f"{example.stem}-example-"
        ) as temporary:
            subprocess.run(
                [
                    pixi,
                    "run",
                    "--manifest-path",
                    str(ROOT / "pixi.toml"),
                    "mojo",
                    "run",
                    str(example),
                ],
                cwd=temporary,
                check=True,
            )


if __name__ == "__main__":
    main()
