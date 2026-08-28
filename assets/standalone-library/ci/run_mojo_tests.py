#!/usr/bin/env python3
"""Run Mojo TestSuite files with safe Linux x86-64 target features."""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AVX512_FEATURES = (
    "-avx512f,-avx512bw,-avx512cd,-avx512dq,-avx512vl,-avx512ifma,"
    "-avx512vbmi,-avx512vbmi2,-avx512vnni,-avx512bitalg,"
    "-avx512vpopcntdq,-avx512fp16,-avx512bf16"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="tests")
    parser.add_argument("--pattern", default="test_*.mojo")
    args = parser.parse_args()

    directory = (ROOT / args.directory).resolve()
    if not directory.is_relative_to(ROOT) or not directory.is_dir():
        raise SystemExit(
            f"Test directory is unavailable or outside the project: {directory}"
        )
    tests = sorted(directory.rglob(args.pattern))
    if not tests:
        raise SystemExit(f"No Mojo tests matched {args.pattern!r} below {directory}")

    mojo = shutil.which("mojo") or "mojo"
    target_args: list[str] = []
    if platform.system() == "Linux" and platform.machine().lower() in {
        "amd64",
        "x86_64",
    }:
        target_args = [f"--target-features={AVX512_FEATURES}"]
    for test in tests:
        subprocess.run(
            [mojo, "run", *target_args, "-D", "ASSERT=all", str(test)],
            cwd=ROOT,
            check=True,
        )


if __name__ == "__main__":
    main()
