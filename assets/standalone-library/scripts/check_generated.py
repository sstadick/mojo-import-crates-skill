#!/usr/bin/env python3
"""Verify checked-in Rust-to-Mojo binding provenance without regenerating it."""

from __future__ import annotations

import hashlib
import json
import stat
from collections import Counter
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
BINDINGS = ROOT / "vendor" / "rust-bindings"
ALLOWED_EXPORT_STATUSES = {
    "ADAPTED",
    "DIRECT",
    "MONOMORPHIZED",
    "OPAQUE",
    "SKIPPED",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_generated_manifest(manifest_path: Path, errors: list[str]) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent.resolve()

    for relative, expected in manifest.get("generated_files", {}).items():
        path = (base / relative).resolve()
        if not path.is_relative_to(base):
            errors.append(
                f"{manifest_path}: generated path escapes its manifest: {relative}"
            )
            continue
        if not path.is_file():
            errors.append(f"{manifest_path}: missing generated file: {relative}")
            continue
        actual = sha256(path)
        if actual != expected:
            errors.append(
                f"{manifest_path}: hash mismatch for {relative}: "
                f"expected {expected}, found {actual}"
            )

    for relative, expected in manifest.get("generated_modes", {}).items():
        path = (base / relative).resolve()
        if not path.exists():
            continue
        actual = stat.S_IMODE(path.stat().st_mode)
        if actual != expected:
            errors.append(
                f"{manifest_path}: mode mismatch for {relative}: "
                f"expected {expected:o}, found {actual:o}"
            )


def validate_binding(binding_path: Path, errors: list[str]) -> tuple[int, Counter[str]]:
    binding = tomllib.loads(binding_path.read_text(encoding="utf-8"))
    exports = binding.get("exports", [])
    export_ids = {item.get("id") for item in exports}
    statuses: Counter[str] = Counter()

    if not exports:
        errors.append(f"{binding_path}: export ledger is empty")

    for item in exports:
        export_id = item.get("id", "<missing id>")
        status = item.get("status")
        statuses[str(status)] += 1
        if status not in ALLOWED_EXPORT_STATUSES:
            errors.append(f"{binding_path}: {export_id} has invalid status {status!r}")
        if not str(item.get("reason", "")).strip():
            errors.append(f"{binding_path}: {export_id} has no audit reason")

    for section in ("specializations", "adaptations"):
        for item in binding.get(section, []):
            if item.get("chosen_by") != "user":
                errors.append(
                    f"{binding_path}: {section} entry is not marked chosen_by=user"
                )
            referenced = item.get("exports", [item.get("export")])
            for export_id in referenced:
                if export_id not in export_ids:
                    errors.append(
                        f"{binding_path}: {section} references unknown export {export_id!r}"
                    )

    return len(exports), statuses


def main() -> None:
    if not BINDINGS.is_dir():
        raise SystemExit(
            "Generated binding integrity failed: vendor/rust-bindings is missing; "
            "finish binding integration before running this check."
        )

    errors: list[str] = []
    manifests = [BINDINGS / "manifest.json", *sorted(BINDINGS.glob("*/manifest.json"))]
    bindings = sorted(BINDINGS.glob("*/binding.toml"))
    reports = sorted(BINDINGS.glob("*/abi-report.json"))

    for manifest_path in manifests:
        if not manifest_path.is_file():
            errors.append(f"missing generated manifest: {manifest_path}")
            continue
        validate_generated_manifest(manifest_path, errors)

    export_count = 0
    statuses: Counter[str] = Counter()
    for binding_path in bindings:
        binding_exports, binding_statuses = validate_binding(binding_path, errors)
        export_count += binding_exports
        statuses.update(binding_statuses)

    function_count = 0
    for report_path in reports:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        unsupported = report.get("unsupported", [])
        if unsupported:
            errors.append(
                f"{report_path}: contains {len(unsupported)} unsupported ABI items"
            )
        function_count += len(report.get("functions", []))

    if errors:
        raise SystemExit(
            "Generated binding integrity failed:\n- " + "\n- ".join(errors)
        )

    status_summary = ", ".join(f"{key}={statuses[key]}" for key in sorted(statuses))
    print(
        f"Generated binding integrity passed: {len(manifests)} manifests, "
        f"{export_count} exports ({status_summary}), {function_count} ABI functions."
    )


if __name__ == "__main__":
    main()
