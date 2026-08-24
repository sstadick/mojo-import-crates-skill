#!/usr/bin/env python3
"""Vendor one generated Rust-to-Mojo binding into a Hat-style Pixi project.

The semantic generator owns ``binding.toml`` and all Rust/Mojo source in the
staging tree. Before copying, this script requires the sibling wrapper
generator's closed manifest/report/raw/output validation to pass. It otherwise
understands only the small integration schema needed to build the aggregate
Pixi source package and does not reinterpret semantic decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Iterable


GENERATOR_NAME = "rust-mojo-binding-integrator"
GENERATOR_VERSION = "0.1.0"
GENERATOR_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SUPPORTED_PLATFORMS = {"linux-64", "linux-aarch64", "osx-arm64"}

_SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)]\s*(?:#.*)?$")
_KEY_RE_TEMPLATE = r'^\s*(?:"{quoted}"|{bare})\s*='
_BINDING_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CRATE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_EXACT_VERSION_RE = re.compile(
    r"^=[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_TOOL_VERSION_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$"
)
_TOOL_KEYS = (
    "generator_version",
    "abi_backend_version",
    "diplomat_version",
    "diplomat_core_version",
    "diplomat_runtime_version",
    "mojo_version",
)
_CARGO_TOOL_PACKAGES = {
    "diplomat": "diplomat_version",
    "diplomat_core": "diplomat_core_version",
    "diplomat-runtime": "diplomat_runtime_version",
}
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_IGNORED_SUFFIXES = {".so", ".dylib", ".dll", ".mojoc", ".mojopkg", ".pyc"}
_AGGREGATE_MANIFEST = "manifest.json"
_PUBLISH_WORKFLOW_MINIMUM = (0, 76, 0)
_WRAPPER_GENERATOR = Path(__file__).resolve().with_name("generate_mojo_package.py")


class IntegrationError(RuntimeError):
    """A deterministic error that can be corrected by the caller."""


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalized_mode(path: Path) -> int:
    """Preserve only executability; avoid host umask and group-mode drift."""

    return 0o755 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o644


def load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise IntegrationError(f"Missing required file: {path}") from error
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise IntegrationError(f"Cannot read TOML from {path}: {error}") from error


def _required_table(document: dict[str, Any], name: str, source: Path) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise IntegrationError(f"{source} requires a [{name}] table")
    return value


def _required_string(table: dict[str, Any], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise IntegrationError(f"{label} must be a non-empty string")
    return value


def _validated_tools(document: dict[str, Any], source: Path) -> dict[str, str]:
    """Validate the closed toolchain claim carried by a generated binding."""

    tools = _required_table(document, "tools", source)
    missing = sorted(set(_TOOL_KEYS) - set(tools))
    unknown = sorted(set(tools) - set(_TOOL_KEYS))
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if unknown:
            details.append("unknown keys: " + ", ".join(unknown))
        raise IntegrationError(
            f"{source} [tools] must be a closed table containing exactly "
            + ", ".join(_TOOL_KEYS)
            + " ("
            + "; ".join(details)
            + ")"
        )
    validated: dict[str, str] = {}
    for key in _TOOL_KEYS:
        value = _required_string(tools, key, f"[tools].{key}")
        if _TOOL_VERSION_RE.fullmatch(value) is None:
            raise IntegrationError(
                f"[tools].{key} must be an exact semver-like version"
            )
        validated[key] = value
    return validated


def _optional_dependency_array(
    table: dict[str, Any], key: str, label: str
) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list):
        raise IntegrationError(f"{label} must be an array of dependency strings")
    dependencies: list[str] = []
    for index, dependency in enumerate(value):
        if (
            not isinstance(dependency, str)
            or not dependency.strip()
            or dependency != dependency.strip()
            or any(character in dependency for character in "\r\n\0")
        ):
            raise IntegrationError(
                f"{label}[{index}] must be a non-empty, single-line dependency string"
            )
        if dependency.split(maxsplit=1)[0] == "mojo-compiler":
            raise IntegrationError(
                f"{label} must not override mojo-compiler; its constraint comes "
                "from the target project's package requirements"
            )
        dependencies.append(dependency)
    return sorted(set(dependencies))


def safe_relative_path(value: Any, label: str) -> str:
    """Return a normalized portable path, rejecting traversal and shell syntax."""

    if not isinstance(value, str) or not value:
        raise IntegrationError(f"{label} must be a non-empty relative path")
    if "\\" in value or value.startswith("/") or value.endswith("/"):
        raise IntegrationError(f"{label} must be a portable relative path: {value!r}")
    raw_parts = value.split("/")
    if any(
        part in {"", ".", ".."} or _PATH_COMPONENT_RE.fullmatch(part) is None
        for part in raw_parts
    ):
        raise IntegrationError(f"{label} contains an unsafe path component: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise IntegrationError(f"{label} must be relative: {value!r}")
    return path.as_posix()


def _require_inside(root: Path, relative: str, label: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise IntegrationError(f"{label} escapes its binding root: {relative!r}") from error
    return candidate


def _required_string_array(
    table: dict[str, Any], key: str, label: str
) -> list[str]:
    value = table.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str)
        and item
        and item == item.strip()
        and not any(character in item for character in "\r\n\0")
        for item in value
    ):
        raise IntegrationError(f"{label} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise IntegrationError(f"{label} must not contain duplicates")
    return sorted(value)


def _cargo_dependency_tables(
    document: dict[str, Any], source: Path
) -> Iterable[tuple[str, dict[str, Any], bool]]:
    """Yield Cargo dependency tables and whether they affect the library itself."""

    for key in ("dependencies", "build-dependencies", "dev-dependencies"):
        table = document.get(key, {})
        if not isinstance(table, dict):
            raise IntegrationError(f"{source} [{key}] must be a table")
        yield key, table, key == "dependencies"
    targets = document.get("target", {})
    if not isinstance(targets, dict):
        raise IntegrationError(f"{source} [target] must be a table")
    for target_name, target in sorted(targets.items()):
        if not isinstance(target, dict):
            raise IntegrationError(
                f"{source} [target.{target_name}] must be a table"
            )
        for key in ("dependencies", "build-dependencies", "dev-dependencies"):
            table = target.get(key, {})
            if not isinstance(table, dict):
                raise IntegrationError(
                    f"{source} [target.{target_name}.{key}] must be a table"
                )
            yield f"target.{target_name}.{key}", table, key == "dependencies"


def _dependency_details(
    alias: str, value: Any, label: str
) -> tuple[str, dict[str, Any]]:
    if isinstance(value, str):
        return alias, {"version": value}
    if not isinstance(value, dict):
        raise IntegrationError(
            f"Cargo dependency {label}.{alias} must be a version string or table"
        )
    package = value.get("package", alias)
    if not isinstance(package, str) or not package:
        raise IntegrationError(
            f"Cargo dependency {label}.{alias}.package must be a string"
        )
    return package, value


def _parse_lock_dependency(value: Any) -> tuple[str, str | None, str | None] | None:
    """Parse Cargo.lock's `name [version [(source)]]` dependency identity."""

    if not isinstance(value, str) or not value:
        return None
    components = value.split(" ", 2)
    package = components[0]
    if len(components) == 1:
        return package, None, None
    version = components[1]
    if not version:
        return None
    if len(components) == 2:
        return package, version, None
    source = components[2]
    if len(source) < 3 or not source.startswith("(") or not source.endswith(")"):
        return None
    return package, version, source[1:-1]


def _lock_edge_count(
    dependencies: Any,
    lock_packages: list[Any],
    selected_package: dict[str, Any],
) -> int:
    """Count dependency entries that unambiguously select one locked package.

    Cargo omits version/source components only when the remaining identity is
    unambiguous in the lockfile.  Reapplying that rule here is important: a bare
    ``diplomat`` edge must not accidentally validate whichever of several locked
    versions happens to match the tool manifest.
    """

    if not isinstance(dependencies, list):
        return 0
    selected_name = selected_package.get("name")
    selected_version = str(selected_package.get("version"))
    selected_source = selected_package.get("source")
    count = 0
    for dependency in dependencies:
        parsed = _parse_lock_dependency(dependency)
        if parsed is None:
            continue
        package, version, source = parsed
        candidates = [
            item
            for item in lock_packages
            if isinstance(item, dict)
            and item.get("name") == package
            and (version is None or str(item.get("version")) == version)
            and (source is None or item.get("source") == source)
        ]
        if len(candidates) == 1 and candidates[0] is selected_package:
            count += 1
    # Spell these comparisons out to make malformed selected package records
    # fail closed instead of being accepted by object identity alone.
    if (
        not isinstance(selected_name, str)
        or not selected_name
        or selected_version in {"", "None"}
        or (selected_source is not None and not isinstance(selected_source, str))
    ):
        return 0
    return count


def _select_locked_tool_package(
    cargo_lock: Path,
    lock_packages: list[Any],
    package: str,
    version: str,
    tool_key: str,
) -> dict[str, Any]:
    """Select the exact registry package named by one closed tool claim."""

    named_registry_packages = [
        item
        for item in lock_packages
        if isinstance(item, dict)
        and item.get("name") == package
        and isinstance(item.get("source"), str)
        and item["source"].startswith("registry+")
    ]
    matches = [
        item
        for item in named_registry_packages
        if str(item.get("version")) == version
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches and len(named_registry_packages) == 1:
        raise IntegrationError(
            f"{cargo_lock} locked {package} version does not match "
            f"[tools].{tool_key}"
        )
    raise IntegrationError(
        f"{cargo_lock} must contain exactly one locked {package!r} package at "
        f"registry version {version} matching [tools].{tool_key}"
    )


def _audit_cargo_graph(
    root: Path,
    cargo_path: Path,
    cargo_lock: Path,
    *,
    crate_name: str,
    crate_version: str,
    source_kind: str,
    crate_checksum: str | None,
    crate_git: str | None,
    crate_rev: str | None,
    features: list[str],
    default_features: bool,
    ffi_crate: str,
    tools: dict[str, str],
) -> dict[str, Any]:
    """Statically bind source provenance to the locked companion Cargo graph.

    Cargo metadata would require fetching synthetic/unit-test registries.  The
    subset checked here is the complete direct dependency declaration plus the
    corresponding Cargo.lock package, which is deterministic and offline.
    """

    document = load_toml(cargo_path)
    package = _required_table(document, "package", cargo_path)
    package_name = _required_string(package, "name", f"{cargo_path} [package].name")
    package_version = _required_string(
        package, "version", f"{cargo_path} [package].version"
    )
    library = _required_table(document, "lib", cargo_path)
    crate_types = library.get("crate-type")
    if not isinstance(crate_types, list) or "cdylib" not in crate_types:
        raise IntegrationError(
            f"{cargo_path} [lib].crate-type must include \"cdylib\""
        )
    library_name = library.get("name", package_name.replace("-", "_"))
    if library_name != ffi_crate:
        raise IntegrationError(
            f"{cargo_path} builds library {library_name!r}, but [ffi].crate_name "
            f"is {ffi_crate!r}"
        )

    declarations_by_package: dict[
        str, list[tuple[str, str, dict[str, Any], bool]]
    ] = {}
    for table_name, table, affects_library in _cargo_dependency_tables(
        document, cargo_path
    ):
        for alias, value in sorted(table.items()):
            package_dependency, details = _dependency_details(
                alias, value, f"[{table_name}]"
            )
            dependency_path = details.get("path")
            if dependency_path is not None:
                if not isinstance(dependency_path, str) or not dependency_path:
                    raise IntegrationError(
                        f"Cargo dependency [{table_name}].{alias}.path must be a string"
                    )
                candidate = Path(dependency_path)
                if candidate.is_absolute():
                    resolved_path = candidate.resolve()
                else:
                    resolved_path = (cargo_path.parent / candidate).resolve()
                try:
                    resolved_path.relative_to(root.resolve())
                except ValueError as error:
                    raise IntegrationError(
                        f"Cargo dependency [{table_name}].{alias} is an external path "
                        f"dependency ({dependency_path!r}); the isolated build context "
                        "contains only the staged binding"
                    ) from error
            declarations_by_package.setdefault(package_dependency, []).append(
                (table_name, alias, details, affects_library)
            )

    selected_tool_declarations: dict[
        str, tuple[str, str, dict[str, Any], bool]
    ] = {}
    for package_dependency in ("diplomat", "diplomat-runtime"):
        declarations = declarations_by_package.get(package_dependency, [])
        tool_key = _CARGO_TOOL_PACKAGES[package_dependency]
        expected_version = "=" + tools[tool_key]
        # An upstream crate may itself be named diplomat or diplomat-runtime.
        # In that case its declaration is a different role, normally at a
        # different version.  Select the exact registry declaration claimed by
        # [tools] before enforcing the direct-dependency invariants.
        candidates = [
            declaration
            for declaration in declarations
            if declaration[2].get("version") == expected_version
            and declaration[2].get("git") is None
            and declaration[2].get("path") is None
            and not declaration[2].get("workspace", False)
        ]
        if len(candidates) != 1:
            raise IntegrationError(
                f"{cargo_path} must directly depend exactly once on "
                f"{package_dependency!r} at {expected_version!r} from its root "
                f"[dependencies] table to match [tools].{tool_key}"
            )
        table_name, alias, dependency, affects_library = candidates[0]
        selected_tool_declarations[package_dependency] = candidates[0]
        if table_name != "dependencies":
            raise IntegrationError(
                f"Cargo dependency [{table_name}].{alias} must be declared in the "
                "root [dependencies] table"
            )
        if not affects_library:
            raise IntegrationError(
                f"Cargo dependency [{table_name}].{alias} must affect the companion library"
            )
        optional = dependency.get("optional", False)
        if not isinstance(optional, bool) or optional:
            raise IntegrationError(
                f"Cargo dependency [{table_name}].{alias} must be non-optional"
            )
        if any(dependency.get(key) is not None for key in ("git", "path")) or dependency.get(
            "workspace", False
        ):
            raise IntegrationError(
                f"Cargo dependency [{table_name}].{alias} must be an exact registry "
                f"dependency matching [tools].{tool_key}"
            )

    upstream_declarations = list(declarations_by_package.get(crate_name, []))
    selected_same_name_tool = selected_tool_declarations.get(crate_name)
    if selected_same_name_tool is not None:
        dependency = selected_same_name_tool[2]
        if source_kind == "registry":
            same_source_identity = (
                dependency.get("version") == crate_version
                and dependency.get("git") is None
                and dependency.get("path") is None
                and not dependency.get("workspace", False)
            )
        else:
            same_source_identity = (
                dependency.get("git") == crate_git
                and dependency.get("rev") == crate_rev
                and dependency.get("path") is None
                and dependency.get("version") in {None, crate_version}
            )
        if not same_source_identity:
            # The declaration selected for the generator's tool dependency is
            # not also the requested upstream package.  Keep the two semantic
            # roles separate even though their Cargo package names coincide.
            upstream_declarations.remove(selected_same_name_tool)

    if not upstream_declarations or not any(
        affects_library for _, _, _, affects_library in upstream_declarations
    ):
        raise IntegrationError(
            f"{cargo_path} must directly depend on upstream package {crate_name!r} "
            "from a [dependencies] table"
        )
    for table_name, alias, dependency, _ in upstream_declarations:
        if dependency.get("optional", False):
            raise IntegrationError(
                f"Upstream dependency [{table_name}].{alias} may not be optional"
            )
        dependency_features = dependency.get("features", [])
        if not isinstance(dependency_features, list) or not all(
            isinstance(feature, str) and feature for feature in dependency_features
        ):
            raise IntegrationError(
                f"Cargo dependency [{table_name}].{alias}.features must be an array"
            )
        if sorted(set(dependency_features)) != features:
            raise IntegrationError(
                f"Cargo dependency [{table_name}].{alias} features do not match "
                "[crate].features"
            )
        dependency_default = dependency.get("default-features", True)
        if not isinstance(dependency_default, bool) or dependency_default != default_features:
            raise IntegrationError(
                f"Cargo dependency [{table_name}].{alias} default-features does not "
                "match [crate].default_features"
            )
        if source_kind == "registry":
            if dependency.get("git") is not None or dependency.get("path") is not None:
                raise IntegrationError(
                    f"Registry upstream [{table_name}].{alias} may not set git/path"
                )
            if dependency.get("version") != crate_version:
                raise IntegrationError(
                    f"Cargo dependency [{table_name}].{alias}.version must exactly "
                    f"match {crate_version!r}"
                )
        else:
            if dependency.get("path") is not None:
                raise IntegrationError(
                    f"Git upstream [{table_name}].{alias} may not set path"
                )
            if dependency.get("git") != crate_git or dependency.get("rev") != crate_rev:
                raise IntegrationError(
                    f"Cargo dependency [{table_name}].{alias} git/rev do not match "
                    "[crate] source identity"
                )
            dependency_version = dependency.get("version")
            if dependency_version is not None and dependency_version != crate_version:
                raise IntegrationError(
                    f"Cargo dependency [{table_name}].{alias}.version must be absent "
                    f"or exactly {crate_version!r}"
                )

    lock_document = load_toml(cargo_lock)
    lock_packages = lock_document.get("package")
    if not isinstance(lock_packages, list):
        raise IntegrationError(f"{cargo_lock} must contain [[package]] entries")
    root_packages = [
        item
        for item in lock_packages
        if isinstance(item, dict)
        and item.get("name") == package_name
        and str(item.get("version")) == package_version
        and item.get("source") is None
    ]
    if len(root_packages) != 1:
        raise IntegrationError(
            f"{cargo_lock} must contain exactly one local companion package "
            f"{package_name}@{package_version}"
        )
    locked_dependencies = root_packages[0].get("dependencies", [])
    selected_tool_packages = {
        package_dependency: _select_locked_tool_package(
            cargo_lock,
            lock_packages,
            package_dependency,
            tools[tool_key],
            tool_key,
        )
        for package_dependency, tool_key in _CARGO_TOOL_PACKAGES.items()
    }

    plain_version = crate_version[1:]
    upstream_packages = [
        item
        for item in lock_packages
        if isinstance(item, dict)
        and item.get("name") == crate_name
        and str(item.get("version")) == plain_version
    ]
    if len(upstream_packages) == 1:
        locked_upstream = upstream_packages[0]
    elif source_kind == "registry":
        source_matches = [
            item
            for item in upstream_packages
            if isinstance(item.get("source"), str)
            and item["source"].startswith("registry+")
            and item.get("checksum") == crate_checksum
        ]
        if len(source_matches) != 1:
            raise IntegrationError(
                f"{cargo_lock} must contain exactly one registry "
                f"{crate_name}@{plain_version} package matching [crate].checksum"
            )
        locked_upstream = source_matches[0]
    else:
        source_matches = []
        for item in upstream_packages:
            source = item.get("source")
            if not isinstance(source, str) or not source.startswith("git+") or "#" not in source:
                continue
            locked_url_and_query, locked_commit = source[4:].rsplit("#", 1)
            locked_url = locked_url_and_query.split("?", 1)[0]
            if locked_url == crate_git and locked_commit == crate_rev:
                source_matches.append(item)
        if len(source_matches) != 1:
            raise IntegrationError(
                f"{cargo_lock} must contain exactly one Git "
                f"{crate_name}@{plain_version} package matching [crate] source identity"
            )
        locked_upstream = source_matches[0]
    if not upstream_packages:
        raise IntegrationError(
            f"{cargo_lock} must contain exactly one {crate_name}@{plain_version} package"
        )
    locked_source = locked_upstream.get("source")
    if not isinstance(locked_source, str) or not locked_source:
        raise IntegrationError(
            f"{cargo_lock} upstream package {crate_name!r} lacks a source"
        )
    if source_kind == "registry":
        if not locked_source.startswith("registry+"):
            raise IntegrationError(
                f"{cargo_lock} upstream source is not a registry source"
            )
        if locked_upstream.get("checksum") != crate_checksum:
            raise IntegrationError(
                f"{cargo_lock} upstream checksum does not match [crate].checksum"
            )
    else:
        if not locked_source.startswith("git+") or "#" not in locked_source:
            raise IntegrationError(f"{cargo_lock} upstream source is not a locked Git source")
        locked_url_and_query, locked_commit = locked_source[4:].rsplit("#", 1)
        locked_url = locked_url_and_query.split("?", 1)[0]
        if locked_url != crate_git or locked_commit != crate_rev:
            raise IntegrationError(
                f"{cargo_lock} Git URL/commit do not match [crate] source identity"
            )

    if _lock_edge_count(locked_dependencies, lock_packages, locked_upstream) != 1:
        raise IntegrationError(
            f"{cargo_lock} companion package does not lock exactly one direct "
            f"dependency on upstream {crate_name!r} at {plain_version}"
        )
    for package_dependency in ("diplomat", "diplomat-runtime"):
        tool_key = _CARGO_TOOL_PACKAGES[package_dependency]
        if (
            _lock_edge_count(
                locked_dependencies,
                lock_packages,
                selected_tool_packages[package_dependency],
            )
            != 1
        ):
            raise IntegrationError(
                f"{cargo_lock} companion package does not lock exactly one direct "
                f"{package_dependency!r} dependency matching [tools].{tool_key}"
            )

    diplomat_dependencies = selected_tool_packages["diplomat"].get(
        "dependencies", []
    )
    if (
        _lock_edge_count(
            diplomat_dependencies,
            lock_packages,
            selected_tool_packages["diplomat_core"],
        )
        != 1
    ):
        raise IntegrationError(
            f"{cargo_lock} selected diplomat package does not lock exactly one "
            "diplomat_core dependency matching [tools].diplomat_core_version"
        )
    return {
        "package_name": package_name,
        "package_version": package_version,
        "cargo_lock": cargo_lock.relative_to(root).as_posix(),
        "upstream_source": locked_source,
    }


def _rust_code_without_comments_or_literals(source: str) -> str:
    """Blank Rust comments and literals while preserving code/newline positions."""

    rendered = list(source)

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if rendered[position] not in {"\n", "\r"}:
                rendered[position] = " "

    index = 0
    length = len(source)
    while index < length:
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            if end < 0:
                end = length
            blank(index, end)
            index = end
            continue
        if source.startswith("/*", index):
            depth = 1
            end = index + 2
            while end < length and depth:
                if source.startswith("/*", end):
                    depth += 1
                    end += 2
                elif source.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            blank(index, end)
            index = end
            continue

        raw = re.match(r'(?:b|c)?r(#{0,255})"', source[index:])
        if raw is not None and (
            index == 0
            or not (source[index - 1].isalnum() or source[index - 1] == "_")
        ):
            terminator = '"' + raw.group(1)
            body_start = index + raw.end()
            close = source.find(terminator, body_start)
            end = length if close < 0 else close + len(terminator)
            blank(index, end)
            index = end
            continue

        if source[index] == '"':
            end = index + 1
            escaped = False
            while end < length:
                character = source[end]
                end += 1
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    break
            blank(index, end)
            index = end
            continue

        if source[index] == "'":
            # A plain char has one scalar; escaped chars begin with a
            # backslash. This deliberately does not consume lifetimes such as
            # `'a` or `'static`.
            if index + 2 < length and source[index + 2] == "'":
                blank(index, index + 3)
                index += 3
                continue
            if index + 1 < length and source[index + 1] == "\\":
                end = index + 2
                while end < min(length, index + 18) and source[end] not in "\r\n":
                    if source[end] == "'":
                        end += 1
                        blank(index, end)
                        index = end
                        break
                    end += 1
                else:
                    index += 1
                continue
        index += 1
    return "".join(rendered)


def _rust_integration_test_target(
    root: Path, cargo_path: Path, relative: str, label: str
) -> str:
    test_path = _require_inside(root, relative, label)
    if not test_path.is_file():
        raise IntegrationError(f"Rust FFI test does not exist: {test_path}")
    try:
        cargo_relative = test_path.resolve().relative_to(cargo_path.parent.resolve())
    except ValueError as error:
        raise IntegrationError(
            f"{label} must be inside the companion Cargo crate"
        ) from error
    if (
        len(cargo_relative.parts) != 2
        or cargo_relative.parts[0] != "tests"
        or cargo_relative.suffix != ".rs"
    ):
        raise IntegrationError(
            f"{label} must name a direct Cargo integration test such as "
            f"{cargo_path.parent.relative_to(root).as_posix()}/tests/smoke.rs"
        )
    try:
        source = test_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise IntegrationError(f"Cannot inspect Rust FFI test {test_path}: {error}") from error
    code = _rust_code_without_comments_or_literals(source)
    if re.search(r"#\s*\[\s*test\s*]\s*(?:pub\s+)?fn\s+[A-Za-z_]", code) is None:
        raise IntegrationError(
            f"{label} must contain at least one explicit #[test] function"
        )
    return cargo_relative.stem


def _validate_generated_mojo_contract(
    root: Path, binding_manifest: Path, mojo_path: Path
) -> str:
    """Require the closed manifest/report/raw/wrapper contract before packaging."""

    report = root / "abi-report.json"
    if report.is_symlink() or not report.is_file():
        raise IntegrationError(
            f"Binding staging root requires a real ABI report: {report}"
        )
    if not _WRAPPER_GENERATOR.is_file():
        raise IntegrationError(
            f"Cannot locate the semantic wrapper validator: {_WRAPPER_GENERATOR}"
        )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(_WRAPPER_GENERATOR),
                "--binding",
                str(binding_manifest),
                "--report",
                str(report),
                "--output-dir",
                str(mojo_path),
                "--check",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise IntegrationError(
            f"Could not run the semantic wrapper validator: {error}"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if detail.startswith("error: "):
            detail = detail.removeprefix("error: ")
        raise IntegrationError(
            "Generated Mojo contract validation failed"
            + (f": {detail}" if detail else "")
        )
    return report.relative_to(root).as_posix()


def validate_binding_root(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise IntegrationError(f"Binding staging root is not a directory: {root}")
    source = root / "binding.toml"
    document = load_toml(source)
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise IntegrationError(f"{source} requires top-level schema_version = 1")

    binding = _required_table(document, "binding", source)
    crate = _required_table(document, "crate", source)
    ffi = _required_table(document, "ffi", source)
    mojo = _required_table(document, "mojo", source)
    tools = _validated_tools(document, source)
    pixi = document.get("pixi", {})
    if not isinstance(pixi, dict):
        raise IntegrationError(f"{source} [pixi] must be a table when present")

    binding_id = _required_string(binding, "id", "[binding].id")
    if _BINDING_ID_RE.fullmatch(binding_id) is None:
        raise IntegrationError(
            "[binding].id must start with a lowercase letter and contain only "
            "lowercase letters, digits, underscores, or hyphens"
        )
    mojo_package = _required_string(
        binding, "mojo_package", "[binding].mojo_package"
    )
    if _IDENTIFIER_RE.fullmatch(mojo_package) is None:
        raise IntegrationError("[binding].mojo_package must be a Mojo identifier")

    crate_name = _required_string(crate, "name", "[crate].name")
    if _CRATE_NAME_RE.fullmatch(crate_name) is None:
        raise IntegrationError("[crate].name is not a valid Cargo package name")
    crate_version = _required_string(crate, "version", "[crate].version")
    if _EXACT_VERSION_RE.fullmatch(crate_version) is None:
        raise IntegrationError(
            "[crate].version must be an exact Cargo version beginning with '='"
        )
    source_kind = _required_string(crate, "source_kind", "[crate].source_kind")
    if source_kind not in {"registry", "git"}:
        if source_kind == "path":
            raise IntegrationError(
                "Local path crate packaging is deferred until a deterministic "
                "source snapshot policy is implemented"
            )
        raise IntegrationError("[crate].source_kind must be 'registry' or 'git'")
    crate_features = _required_string_array(
        crate, "features", "[crate].features"
    )
    default_features = crate.get("default_features")
    if not isinstance(default_features, bool):
        raise IntegrationError("[crate].default_features must be a boolean")
    crate_checksum = crate.get("checksum")
    crate_git = crate.get("git")
    crate_rev = crate.get("rev")
    if source_kind == "registry":
        if not isinstance(crate_checksum, str) or re.fullmatch(
            r"[0-9a-f]{64}", crate_checksum
        ) is None:
            raise IntegrationError(
                "Registry [crate].checksum must be 64 lowercase hexadecimal characters"
            )
        if crate_git is not None or crate_rev is not None:
            raise IntegrationError("Registry [crate] must not set git or rev")
    else:
        if not isinstance(crate_git, str) or not crate_git:
            raise IntegrationError("Git [crate].git must be a non-empty URL")
        if not isinstance(crate_rev, str) or re.fullmatch(
            r"[0-9a-f]{40}", crate_rev
        ) is None:
            raise IntegrationError(
                "Git [crate].rev must be a full lowercase commit hash"
            )
        if crate_checksum is not None:
            raise IntegrationError("Git [crate] must not set checksum")

    ffi_crate = _required_string(ffi, "crate_name", "[ffi].crate_name")
    if _IDENTIFIER_RE.fullmatch(ffi_crate) is None:
        raise IntegrationError(
            "[ffi].crate_name must be the Rust library identifier used in its filename"
        )
    cargo_manifest = safe_relative_path(
        ffi.get("cargo_manifest"), "[ffi].cargo_manifest"
    )
    cargo_path = _require_inside(root, cargo_manifest, "[ffi].cargo_manifest")
    if not cargo_path.is_file():
        raise IntegrationError(f"Cargo manifest does not exist: {cargo_path}")
    lock_search = cargo_path.parent
    cargo_lock: Path | None = None
    while True:
        candidate = lock_search / "Cargo.lock"
        if candidate.is_file():
            cargo_lock = candidate
            break
        if lock_search == root:
            break
        lock_search = lock_search.parent
    if cargo_lock is None:
        raise IntegrationError(
            f"No Cargo.lock exists between {cargo_path.parent} and {root}; "
            "the aggregate recipe always builds with --locked"
        )

    ffi_tests_value = ffi.get("tests")
    if not isinstance(ffi_tests_value, list) or not ffi_tests_value:
        raise IntegrationError("[ffi].tests must be a non-empty array of relative paths")
    ffi_tests: list[str] = []
    rust_test_targets: list[str] = []
    for index, value in enumerate(ffi_tests_value):
        relative = safe_relative_path(value, f"[ffi].tests[{index}]")
        ffi_tests.append(relative)
        rust_test_targets.append(
            _rust_integration_test_target(
                root, cargo_path, relative, f"[ffi].tests[{index}]"
            )
        )
    if len(set(ffi_tests)) != len(ffi_tests) or len(set(rust_test_targets)) != len(
        rust_test_targets
    ):
        raise IntegrationError("[ffi].tests must identify distinct integration tests")

    cargo_graph = _audit_cargo_graph(
        root,
        cargo_path,
        cargo_lock,
        crate_name=crate_name,
        crate_version=crate_version,
        source_kind=source_kind,
        crate_checksum=crate_checksum,
        crate_git=crate_git,
        crate_rev=crate_rev,
        features=crate_features,
        default_features=default_features,
        ffi_crate=ffi_crate,
        tools=tools,
    )

    mojo_source = safe_relative_path(mojo.get("source_dir"), "[mojo].source_dir")
    mojo_path = _require_inside(root, mojo_source, "[mojo].source_dir")
    if not mojo_path.is_dir():
        raise IntegrationError(f"Mojo source directory does not exist: {mojo_path}")
    if not (mojo_path / "__init__.mojo").is_file():
        raise IntegrationError(
            f"Mojo package source must contain __init__.mojo: {mojo_path}"
        )
    abi_report = _validate_generated_mojo_contract(root, source, mojo_path)

    tests_value = mojo.get("tests")
    if not isinstance(tests_value, list) or not tests_value:
        raise IntegrationError("[mojo].tests must be a non-empty array of relative paths")
    tests: list[str] = []
    for index, value in enumerate(tests_value):
        relative = safe_relative_path(value, f"[mojo].tests[{index}]")
        test_path = _require_inside(root, relative, f"[mojo].tests[{index}]")
        if not test_path.is_file():
            raise IntegrationError(f"Mojo test does not exist: {test_path}")
        tests.append(relative)
    if len(set(tests)) != len(tests):
        raise IntegrationError("[mojo].tests must not contain duplicate paths")

    return {
        "root": root,
        "document": document,
        "id": binding_id,
        "mojo_package": mojo_package,
        "crate_name": crate_name,
        "crate_version": crate_version,
        "source_kind": source_kind,
        "crate_features": crate_features,
        "default_features": default_features,
        "crate_checksum": crate_checksum,
        "crate_git": crate_git,
        "crate_rev": crate_rev,
        "ffi_crate": ffi_crate,
        "cargo_manifest": cargo_manifest,
        "cargo_lock": cargo_graph["cargo_lock"],
        "cargo_upstream_source": cargo_graph["upstream_source"],
        "tools": tools,
        "ffi_tests": ffi_tests,
        "rust_test_targets": rust_test_targets,
        "mojo_source": mojo_source,
        "abi_report": abi_report,
        "tests": tests,
        "pixi_build_dependencies": _optional_dependency_array(
            pixi, "build_dependencies", "[pixi].build_dependencies"
        ),
        "pixi_host_dependencies": _optional_dependency_array(
            pixi, "host_dependencies", "[pixi].host_dependencies"
        ),
        "pixi_run_dependencies": _optional_dependency_array(
            pixi, "run_dependencies", "[pixi].run_dependencies"
        ),
    }


def iter_binding_files(root: Path, cargo_manifest: str) -> Iterable[Path]:
    """Yield deterministic source inputs and reject symlinked generated source."""

    cargo_parent = root.joinpath(*PurePosixPath(cargo_manifest).parts).parent
    ignored_roots = {
        root / "target",
        root / ".pixi",
        root / "artifacts",
        cargo_parent / "target",
    }
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".cargo" in relative.parts:
            raise IntegrationError(
                f"Binding trees may not contain .cargo configuration: {path}"
            )
        if "__pycache__" in relative.parts or any(
            path == ignored or ignored in path.parents for ignored in ignored_roots
        ):
            continue
        if path.is_symlink():
            raise IntegrationError(f"Binding staging trees may not contain symlinks: {path}")
        if not path.is_file() or relative.as_posix() == _AGGREGATE_MANIFEST:
            continue
        if path.suffix in _IGNORED_SUFFIXES:
            raise IntegrationError(
                f"Binding staging tree contains a compiled artifact outside an "
                f"ignored build directory: {path}"
            )
        if path.name == ".DS_Store":
            continue
        yield path


def parse_resolution(value: str | None) -> Any:
    if value is None:
        return None
    source = value
    candidate: Path | None = None
    if value.startswith("@"):
        candidate = Path(value[1:])
    elif not value.lstrip().startswith("{"):
        possible = Path(value)
        if possible.is_file():
            candidate = possible
    if candidate is not None:
        try:
            source = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise IntegrationError(f"Cannot read resolution JSON from {candidate}: {error}") from error
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as error:
        raise IntegrationError(f"--resolution is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise IntegrationError("--resolution JSON must be an object")
    return parsed


def _resolution_string(table: dict[str, Any], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise IntegrationError(f"Resolution {label} must be a non-empty string")
    return value


def _canonical_project_path(value: Any, project: Path, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrationError(f"Resolution {label} must be a non-empty path")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project / candidate
    try:
        relative = candidate.resolve().relative_to(project.resolve())
    except ValueError as error:
        raise IntegrationError(
            f"Resolution {label} is outside the target project. Local crates must "
            "be project-relative until a source snapshot policy is implemented."
        ) from error
    return relative.as_posix()


def canonicalize_resolution(
    value: Any, spec: dict[str, Any], project: Path
) -> dict[str, Any] | None:
    """Validate resolver output and retain only deterministic source identity."""

    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise IntegrationError("Resolution JSON requires schema_version = 1")
    request = value.get("request")
    resolved = value.get("resolved")
    if not isinstance(request, dict) or not isinstance(resolved, dict):
        raise IntegrationError("Resolution JSON requires request and resolved objects")

    kind = _resolution_string(request, "kind", "request.kind")
    if kind not in {"registry", "git", "path"}:
        raise IntegrationError(f"Unsupported resolution request kind: {kind!r}")
    if kind != spec["source_kind"]:
        raise IntegrationError(
            f"Resolution request kind {kind!r} does not match "
            f"[crate].source_kind {spec['source_kind']!r}"
        )
    request_name = _resolution_string(request, "name", "request.name")
    resolved_name = _resolution_string(resolved, "name", "resolved.name")
    resolved_version = _resolution_string(resolved, "version", "resolved.version")
    manifest_version = spec["crate_version"]
    if manifest_version.startswith("="):
        manifest_version = manifest_version[1:]
    if resolved_name != spec["crate_name"] or resolved_version != manifest_version:
        raise IntegrationError(
            "Resolution does not match binding.toml: resolved "
            f"{resolved_name}@{resolved_version}, expected "
            f"{spec['crate_name']}@{manifest_version}"
        )
    if request_name != resolved_name:
        raise IntegrationError(
            f"Resolution request name {request_name!r} does not match resolved "
            f"package {resolved_name!r}"
        )

    features = request.get("features")
    default_features = request.get("default_features")
    if not isinstance(features, list) or not all(
        isinstance(feature, str) and feature for feature in features
    ):
        raise IntegrationError("Resolution request.features must be an array of strings")
    if not isinstance(default_features, bool):
        raise IntegrationError("Resolution request.default_features must be a boolean")
    canonical_features = sorted(set(features))
    if canonical_features != spec["crate_features"]:
        raise IntegrationError(
            "Resolution request.features does not match [crate].features"
        )
    if default_features != spec["default_features"]:
        raise IntegrationError(
            "Resolution request.default_features does not match "
            "[crate].default_features"
        )

    canonical_request: dict[str, Any] = {
        "kind": kind,
        "name": request_name,
        "features": canonical_features,
        "default_features": default_features,
    }
    if kind == "registry":
        request_version = _resolution_string(request, "version", "request.version")
        if request_version.removeprefix("=") != resolved_version:
            raise IntegrationError(
                f"Resolution requested {request_version!r} but resolved {resolved_version!r}"
            )
        canonical_request["version"] = spec["crate_version"]
    elif kind == "git":
        request_git = _resolution_string(request, "git", "request.git")
        revision = _resolution_string(request, "rev", "request.rev")
        if re.fullmatch(r"[0-9a-fA-F]{40}", revision) is None:
            raise IntegrationError("Resolution request.rev must be a full Git commit hash")
        if request_git != spec["crate_git"] or revision.lower() != spec["crate_rev"]:
            raise IntegrationError(
                "Resolution request git/rev does not match [crate] source identity"
            )
        canonical_request["git"] = request_git
        canonical_request["rev"] = revision.lower()
    else:
        # The aggregate source package contains only vendor/rust-bindings. A
        # project-relative upstream crate would be absent from Pixi's isolated
        # build context even though it is safe to name in provenance. Reject it
        # until the generator defines and validates a source-snapshot layout.
        _canonical_project_path(request.get("path"), project, "request.path")
        _canonical_project_path(
            request.get("package_path"), project, "request.package_path"
        )
        _resolution_string(request, "fingerprint", "request.fingerprint")
        raise IntegrationError(
            "Local path crate packaging is deferred until a deterministic source "
            "snapshot policy is implemented"
        )

    source = resolved.get("source")
    checksum = resolved.get("checksum")
    if source is not None and (not isinstance(source, str) or not source):
        raise IntegrationError("Resolution resolved.source must be a string or null")
    if checksum is not None and (not isinstance(checksum, str) or not checksum):
        raise IntegrationError("Resolution resolved.checksum must be a string or null")
    if kind == "registry" and (source is None or checksum is None):
        raise IntegrationError(
            "Registry resolution requires resolved.source and resolved.checksum"
        )
    if kind == "registry" and re.fullmatch(r"[0-9a-f]{64}", checksum or "") is None:
        raise IntegrationError(
            "Registry resolution checksum must be 64 lowercase hexadecimal characters"
        )
    manifest_checksum = spec["crate_checksum"]
    if checksum != manifest_checksum:
        raise IntegrationError(
            "Resolution checksum does not match [crate].checksum: "
            f"{checksum!r} != {manifest_checksum!r}"
        )
    if source != spec["cargo_upstream_source"]:
        raise IntegrationError(
            "Resolution source does not match the upstream package source in Cargo.lock"
        )
    canonical_resolved: dict[str, Any] = {
        "name": resolved_name,
        "version": resolved_version,
        "source": source,
        "checksum": checksum,
    }
    if kind == "git":
        git_commit = _resolution_string(
            resolved, "git_commit", "resolved.git_commit"
        )
        if git_commit.lower() != canonical_request["rev"].lower():
            raise IntegrationError(
                "Resolution resolved.git_commit does not match request.rev"
            )
        canonical_resolved["git_commit"] = git_commit.lower()
    return {
        "schema_version": 1,
        "request": canonical_request,
        "resolved": canonical_resolved,
    }


def _load_json_manifest(path: Path, kind: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IntegrationError(f"Cannot parse existing {kind} {path}: {error}") from error
    if not isinstance(value, dict):
        raise IntegrationError(f"Existing {kind} {path} must contain a JSON object")
    return value


def validate_provenance_manifest(
    manifest: dict[str, Any] | None,
    path: Path,
    *,
    binding_id: str | None = None,
) -> None:
    if manifest is None:
        return
    if path.is_symlink() or not path.is_file() or normalized_mode(path) != 0o644:
        raise IntegrationError(
            f"Existing generated provenance {path} must be a regular mode-0644 file"
        )
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
    ):
        raise IntegrationError(f"Existing generated provenance {path} has wrong schema")
    generator = manifest.get("generator")
    if not isinstance(generator, dict) or generator.get("name") != GENERATOR_NAME:
        raise IntegrationError(
            f"Existing {path} is not owned by {GENERATOR_NAME}; refusing to trust it"
        )
    if binding_id is not None:
        binding = manifest.get("binding")
        if not isinstance(binding, dict) or binding.get("id") != binding_id:
            raise IntegrationError(
                f"Existing {path} does not identify binding {binding_id!r}"
            )


def _previous_hashes(manifest: dict[str, Any] | None, path: Path) -> dict[str, str]:
    if manifest is None:
        return {}
    files = manifest.get("generated_files")
    if not isinstance(files, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in files.items()
    ):
        raise IntegrationError(f"Existing {path} has invalid generated_files")
    normalized: dict[str, str] = {}
    for key, value in files.items():
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise IntegrationError(
                f"Existing {path} has a non-SHA-256 hash for {key!r}"
            )
        normalized[safe_relative_path(key, f"generated_files key in {path}")] = value
    return normalized


def _previous_modes(
    manifest: dict[str, Any] | None,
    path: Path,
    previous_hashes: dict[str, str],
) -> dict[str, int]:
    if manifest is None:
        return {}
    modes = manifest.get("generated_modes")
    if not isinstance(modes, dict):
        raise IntegrationError(f"Existing {path} has invalid generated_modes")
    normalized: dict[str, int] = {}
    for key, value in modes.items():
        relative = safe_relative_path(key, f"generated_modes key in {path}")
        if type(value) is not int or value not in {0o644, 0o755}:
            raise IntegrationError(
                f"Existing {path} has an invalid normalized mode for {key!r}"
            )
        normalized[relative] = value
    if set(normalized) != set(previous_hashes):
        raise IntegrationError(
            f"Existing {path} generated_modes and generated_files inventories differ"
        )
    return normalized


def _filesystem_inventory(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    if not root.exists():
        return files, directories
    if root.is_symlink() or not root.is_dir():
        raise IntegrationError(f"Generated tree root must be a real directory: {root}")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise IntegrationError(f"Generated trees may not contain symlinks: {path}")
        if path.is_dir():
            directories.add(relative)
        elif path.is_file():
            files.add(relative)
        else:
            raise IntegrationError(f"Generated trees may contain only files: {path}")
    return files, directories


def _parent_directories(files: Iterable[str]) -> set[str]:
    directories: set[str] = set()
    for relative in files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def ensure_closed_binding_inventory(
    root: Path,
    previous_hashes: dict[str, str],
    manifest_exists: bool,
    *,
    label: str,
) -> None:
    actual_files, actual_directories = _filesystem_inventory(root)
    expected_files = set(previous_hashes)
    if manifest_exists:
        expected_files.add(_AGGREGATE_MANIFEST)
    expected_directories = _parent_directories(expected_files)
    unexpected = sorted(actual_files - expected_files)
    missing = sorted(expected_files - actual_files)
    unexpected_directories = sorted(actual_directories - expected_directories)
    missing_directories = sorted(expected_directories - actual_directories)
    if unexpected or missing or unexpected_directories or missing_directories:
        details: list[str] = []
        if unexpected:
            details.append("untracked files: " + ", ".join(unexpected))
        if missing:
            details.append("missing tracked files: " + ", ".join(missing))
        if unexpected_directories:
            details.append("untracked directories: " + ", ".join(unexpected_directories))
        if missing_directories:
            details.append("missing tracked directories: " + ", ".join(missing_directories))
        raise IntegrationError(
            f"{label} is not a closed generated tree (" + "; ".join(details) + ")"
        )


def ensure_tracked_files_unmodified(
    root: Path,
    previous_hashes: dict[str, str],
    previous_modes: dict[str, int],
    force: bool,
    *,
    label: str,
) -> None:
    directories = [
        relative
        for relative in previous_hashes
        if root.joinpath(*PurePosixPath(relative).parts).is_dir()
    ]
    if directories:
        raise IntegrationError(
            f"Generated {label} file paths became directories; refusing an unsafe "
            "replacement: " + ", ".join(sorted(directories))
        )
    changed: list[str] = []
    for relative, expected in previous_hashes.items():
        path = root.joinpath(*PurePosixPath(relative).parts)
        if (
            not path.exists()
            or path.is_symlink()
            or not path.is_file()
            or sha256_file(path) != expected
            or normalized_mode(path) != previous_modes[relative]
        ):
            changed.append(relative)
    if changed and not force:
        raise IntegrationError(
            f"Generated {label} files were edited; refusing to overwrite: "
            + ", ".join(sorted(changed))
            + ". Pass --force-generated only if those edits may be discarded."
        )


def _binding_provenance(
    spec: dict[str, Any],
    files: dict[str, bytes],
    modes: dict[str, int],
    resolution: Any,
) -> bytes:
    crate_identity: dict[str, Any] = {
        "name": spec["crate_name"],
        "version": spec["crate_version"],
        "source_kind": spec["source_kind"],
        "features": spec["crate_features"],
        "default_features": spec["default_features"],
    }
    if spec["source_kind"] == "registry":
        crate_identity["checksum"] = spec["crate_checksum"]
    else:
        crate_identity["git"] = spec["crate_git"]
        crate_identity["rev"] = spec["crate_rev"]
    payload = {
        "schema_version": 1,
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
            "sha256": GENERATOR_SHA256,
        },
        "binding": {
            "id": spec["id"],
            "mojo_package": spec["mojo_package"],
        },
        "crate": crate_identity,
        "tools": dict(spec["tools"]),
        "ffi": {
            "crate_name": spec["ffi_crate"],
            "cargo_manifest": spec["cargo_manifest"],
            "cargo_lock": spec["cargo_lock"],
            "tests": spec["ffi_tests"],
        },
        "mojo": {"source_dir": spec["mojo_source"], "tests": spec["tests"]},
        "resolution": resolution,
        "generated_files": {
            relative: sha256_bytes(content)
            for relative, content in sorted(files.items())
        },
        "generated_modes": dict(sorted(modes.items())),
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def desired_binding_files(
    spec: dict[str, Any], resolution: Any
) -> tuple[dict[str, bytes], dict[str, int]]:
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    for path in iter_binding_files(spec["root"], spec["cargo_manifest"]):
        relative = path.relative_to(spec["root"]).as_posix()
        files[relative] = path.read_bytes()
        modes[relative] = normalized_mode(path)
    if "binding.toml" not in files:
        raise IntegrationError("Binding staging tree does not contain binding.toml")
    files[_AGGREGATE_MANIFEST] = _binding_provenance(
        spec, files, modes, resolution
    )
    modes[_AGGREGATE_MANIFEST] = 0o644
    return files, modes


def normalized_package_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", name.lower().replace("_", "-"))
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized:
        raise IntegrationError(f"Cannot derive a package name from {name!r}")
    return normalized


def _binding_symbol_prefix(binding_id: str) -> str:
    """Mirror the semantic generator's deterministic C symbol namespace."""

    return "rust_mojo__" + binding_id.replace("-", "_") + "__"


def _binding_loader_environment(binding_id: str) -> str:
    """Mirror the generated Mojo runtime's library override variable."""

    normalized = re.sub(r"[^A-Za-z0-9]", "_", binding_id).upper()
    return "RUST_MOJO_" + normalized + "_LIBRARY"


def section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    start: int | None = None
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if not match:
            continue
        current = match.group(1).strip()
        if start is not None:
            return start, index
        if current == section:
            start = index
    if start is None:
        return None
    return start, len(lines)


def find_key(lines: list[str], bounds: tuple[int, int], key: str) -> int | None:
    pattern = re.compile(
        _KEY_RE_TEMPLATE.format(quoted=re.escape(key), bare=re.escape(key))
    )
    for index in range(bounds[0] + 1, bounds[1]):
        if pattern.match(lines[index]):
            return index
    return None


def parse_single_entry(section: str, line: str) -> Any:
    try:
        parsed = tomllib.loads(f"[{section}]\n{line}\n")
        value: Any = parsed
        for component in section.split("."):
            value = value[component]
        return next(iter(value.values()))
    except (KeyError, StopIteration, tomllib.TOMLDecodeError) as error:
        raise IntegrationError(
            f"Cannot safely parse existing entry in [{section}]: {line.strip()}"
        ) from error


def insert_entry(lines: list[str], section: str, entry: str) -> list[str]:
    result = list(lines)
    bounds = section_bounds(result, section)
    if bounds is None:
        if result and result[-1].strip():
            result.append("")
        result.extend((f"[{section}]", entry))
        return result
    insert_at = bounds[1]
    while insert_at > bounds[0] + 1 and not result[insert_at - 1].strip():
        insert_at -= 1
    result.insert(insert_at, entry)
    return result


def ensure_dependency(
    lines: list[str], section: str, package_name: str, relative_path: str
) -> list[str]:
    expected = {"path": relative_path}
    bounds = section_bounds(lines, section)
    if bounds is not None:
        index = find_key(lines, bounds, package_name)
        if index is not None:
            existing = parse_single_entry(section, lines[index])
            if existing != expected:
                raise IntegrationError(
                    f"[{section}] already defines {package_name!r} as {existing!r}; "
                    f"expected {expected!r}. Refusing to overwrite a user entry."
                )
            return lines
    return insert_entry(
        lines, section, f'"{package_name}" = {{ path = "{relative_path}" }}'
    )


def configure_root_publish(
    lines: list[str], artifact_mode: str, prior_owned: bool
) -> tuple[list[str], list[str], bool]:
    """Apply only the publish setting owned by this integrator.

    A root ``publish`` key that predates integration is user configuration and
    is never overwritten or removed. A key inserted by publish mode is marked
    as owned in aggregate provenance, so a later explicit move to build mode
    can remove it without leaving an old-Pixi-incompatible manifest behind.
    """

    bounds = section_bounds(lines, "package")
    if bounds is None:
        raise IntegrationError("The Pixi manifest has no [package] table")
    index = find_key(lines, bounds, "publish")
    if artifact_mode == "build":
        if index is None:
            return lines, [], False
        value = parse_single_entry("package", lines[index])
        if not isinstance(value, bool):
            raise IntegrationError("[package].publish must be a boolean")
        if prior_owned and value:
            result = list(lines)
            del result[index]
            return result, [], False
        return lines, [
            "Build artifact mode preserved the user-owned [package].publish "
            "setting. Pixi releases that predate publish metadata may reject it."
        ], False
    if artifact_mode != "publish":
        raise IntegrationError(f"Unknown artifact mode: {artifact_mode!r}")
    if index is None:
        return insert_entry(lines, "package", "publish = true"), [], True
    value = parse_single_entry("package", lines[index])
    if not isinstance(value, bool):
        raise IntegrationError("[package].publish must be a boolean")
    if value:
        return lines, [], prior_owned
    raise IntegrationError(
        "Publish artifact mode requires the root [package].publish setting to be true, "
        "but this project explicitly sets publish = false. Set [package].publish = true "
        "to enable workspace publication, or select --artifact-mode build."
    )


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", value)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def infer_artifact_mode(requires_pixi: str | None) -> tuple[str, str]:
    """Conservatively infer the artifact command from ``requires-pixi``.

    The publication package metadata used here is known to work with Pixi
    0.76+. Ambiguous, missing, or older constraints intentionally choose the
    backwards-compatible build manifest. Callers that inspected a concrete
    Pixi executable can always override this with ``--artifact-mode``.
    """

    if requires_pixi is None or "|" in requires_pixi:
        return "build", "compatible-default"
    lower_bounds: list[tuple[int, int, int]] = []
    for constraint in requires_pixi.split(","):
        match = re.fullmatch(
            r"\s*(>=|==|=)?\s*([0-9]+(?:\.[0-9]+){1,2})\s*", constraint
        )
        if match is None:
            continue
        version = _version_tuple(match.group(2))
        if version is not None:
            lower_bounds.append(version)
    if lower_bounds and max(lower_bounds) >= _PUBLISH_WORKFLOW_MINIMUM:
        return "publish", "requires-pixi"
    return "build", "compatible-default"


def validate_project(
    project: Path, requested_artifact_mode: str | None
) -> dict[str, Any]:
    manifest = load_toml(project / "pixi.toml")
    workspace = _required_table(manifest, "workspace", project / "pixi.toml")
    package = _required_table(manifest, "package", project / "pixi.toml")
    preview = workspace.get("preview", [])
    if not isinstance(preview, list) or "pixi-build" not in preview:
        raise IntegrationError('The target workspace must enable preview = ["pixi-build"]')
    requires_pixi = workspace.get("requires-pixi")
    if requires_pixi is not None and not isinstance(requires_pixi, str):
        raise IntegrationError("[workspace].requires-pixi must be a string")
    if requested_artifact_mode is None:
        artifact_mode, artifact_mode_source = infer_artifact_mode(requires_pixi)
    else:
        artifact_mode = requested_artifact_mode
        artifact_mode_source = "explicit"
    platforms = workspace.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        raise IntegrationError("[workspace].platforms must be a non-empty array")
    unsupported = sorted(set(platforms) - SUPPORTED_PLATFORMS)
    if unsupported:
        raise IntegrationError(
            "The binding package supports Linux and Apple silicon macOS, not: "
            + ", ".join(unsupported)
        )
    build = package.get("build")
    backend = build.get("backend") if isinstance(build, dict) else None
    if not isinstance(backend, dict) or backend.get("name") != "pixi-build-mojo":
        raise IntegrationError("The target package must use the pixi-build-mojo backend")
    name = _required_string(package, "name", "[package].name")
    version = _required_string(package, "version", "[package].version")
    mojo_constraints: list[str] = []
    for table_name in (
        "build-dependencies",
        "host-dependencies",
        "run-dependencies",
    ):
        table = package.get(table_name, {})
        value = table.get("mojo-compiler") if isinstance(table, dict) else None
        if isinstance(value, dict):
            value = value.get("version")
        if value is not None:
            if not isinstance(value, str):
                raise IntegrationError(
                    f"[package.{table_name}].mojo-compiler needs a version string"
                )
            mojo_constraints.append(value)
    dependencies = manifest.get("dependencies", {})
    if isinstance(dependencies, dict):
        value = dependencies.get("mojo-compiler", dependencies.get("mojo"))
        if isinstance(value, dict):
            value = value.get("version")
        if isinstance(value, str):
            mojo_constraints.append(value)
    normalized_mojo_versions: list[str] = []
    for constraint in mojo_constraints:
        if constraint.startswith("=="):
            mojo_version = constraint[2:]
        elif constraint.startswith("="):
            mojo_version = constraint[1:]
        else:
            mojo_version = ""
        if _TOOL_VERSION_RE.fullmatch(mojo_version) is None:
            raise IntegrationError(
                "The target project must use one unambiguous exact Mojo compiler "
                "semver-like constraint across its package requirements"
            )
        normalized_mojo_versions.append(mojo_version)
    unique_mojo_versions = sorted(set(normalized_mojo_versions))
    if len(unique_mojo_versions) != 1:
        raise IntegrationError(
            "The target project must use one unambiguous exact Mojo compiler "
            "semver-like constraint across its package requirements"
        )
    mojo_version = unique_mojo_versions[0]
    return {
        "document": manifest,
        "package_name": name,
        "package_version": version,
        "mojo_constraint": "==" + mojo_version,
        "mojo_version": mojo_version,
        "artifact_mode": artifact_mode,
        "artifact_mode_source": artifact_mode_source,
    }


def patch_root_manifest(
    original: str,
    aggregate_package: str,
    artifact_mode: str,
    prior_publish_owned: bool,
) -> tuple[str, list[str], bool]:
    lines, warnings, publish_owned = configure_root_publish(
        original.splitlines(), artifact_mode, prior_publish_owned
    )
    for section in (
        "dependencies",
        "package.build-dependencies",
        "package.run-dependencies",
    ):
        lines = ensure_dependency(
            lines, section, aggregate_package, "vendor/rust-bindings"
        )
    patched = "\n".join(lines) + "\n"
    try:
        tomllib.loads(patched)
    except tomllib.TOMLDecodeError as error:
        raise IntegrationError(f"Generated pixi.toml mutation is invalid: {error}") from error
    return patched, warnings, publish_owned


def render_nested_manifest(package_name: str, version: str, artifact_mode: str) -> str:
    publish = "publish = true\n" if artifact_mode == "publish" else ""
    return f'''# GENERATED FILE - DO NOT EDIT DIRECTLY
# Generator: {GENERATOR_NAME} {GENERATOR_VERSION}

[package]
name = {json.dumps(package_name)}
version = {json.dumps(version)}
description = "Generated Rust-to-Mojo bindings for the parent project."
{publish}

[package.build]
backend = {{ name = "pixi-build-rattler-build", version = "0.*", channels = [
    "https://prefix.dev/pixi-build-backends",
    "https://prefix.dev/conda-forge",
] }}
'''


def render_recipe(
    package_name: str,
    version: str,
    specs: list[dict[str, Any]],
    mojo_constraint: str,
) -> str:
    content_hash = aggregate_content_hash(specs)
    lines = [
        "# GENERATED FILE - DO NOT EDIT DIRECTLY",
        f"# Generator: {GENERATOR_NAME} {GENERATOR_VERSION}",
        "schema_version: 1",
        "",
        "context:",
        f"  name: {json.dumps(package_name)}",
        f"  version: {json.dumps(version)}",
        "",
        "package:",
        "  name: ${{ name }}",
        "  version: ${{ version }}",
        "",
        "source:",
        "  path: .",
        "",
        "build:",
        "  number: 0",
        f"  string: rust_mojo_{content_hash}_h${{{{ hash }}}}_${{{{ build_number }}}}",
        "  skip:",
        "    - win",
        "  script:",
        '    - mkdir -p "$PREFIX/lib" "$PREFIX/lib/mojo" .rust-mojo-target',
    ]
    for spec in sorted(specs, key=lambda item: item["id"]):
        binding_id = spec["id"]
        manifest = f"{binding_id}/{spec['cargo_manifest']}"
        target = f".rust-mojo-target/{binding_id}"
        ffi_crate = spec["ffi_crate"]
        mojo_source = f"{binding_id}/{spec['mojo_source']}"
        mojo_package = spec["mojo_package"]
        for test_target in spec["rust_test_targets"]:
            test_command = (
                'RUSTFLAGS="${RUSTFLAGS:-} '
                "--remap-path-prefix=${SRC_DIR}=/usr/src/rust-mojo-bindings "
                "--remap-path-prefix=${BUILD_PREFIX}=/usr/src/rust-build-prefix\" "
                f"cargo test --release --locked --manifest-path {manifest} "
                f"--target-dir {target} --test {test_target}"
            )
            lines.append(
                "    - "
                + json.dumps(test_command + " -- --list | grep -q ': test$'")
            )
            lines.append(
                "    - " + json.dumps(test_command)
            )
        lines.extend(
            [
                "    - RUSTFLAGS=\"${RUSTFLAGS:-} "
                "--remap-path-prefix=${SRC_DIR}=/usr/src/rust-mojo-bindings "
                "--remap-path-prefix=${BUILD_PREFIX}=/usr/src/rust-build-prefix\" "
                f"cargo build --release --locked --manifest-path {manifest} "
                f"--target-dir {target}",
                "    - if: linux",
                "      then:",
                f'        - install -m 755 "{target}/${{CARGO_BUILD_TARGET:+${{CARGO_BUILD_TARGET}}/}}release/lib{ffi_crate}.so" '
                f'"$PREFIX/lib/lib{ffi_crate}.so"',
                "    - if: osx",
                "      then:",
                f'        - install -m 755 "{target}/${{CARGO_BUILD_TARGET:+${{CARGO_BUILD_TARGET}}/}}release/lib{ffi_crate}.dylib" '
                f'"$PREFIX/lib/lib{ffi_crate}.dylib"',
                f'    - mojo precompile {mojo_source} -o "$PREFIX/lib/mojo/{mojo_package}.mojoc"',
            ]
        )
        for mojo_test in spec["tests"]:
            command = (
                f'MODULAR_HOME="$PREFIX/share/max" "$PREFIX/bin/mojo" run '
                f"{binding_id}/{mojo_test}"
            )
            lines.append(f"    - {json.dumps(command)}")
    extra_build = sorted(
        {
            dependency
            for spec in specs
            for dependency in spec["pixi_build_dependencies"]
        }
    )
    extra_host = sorted(
        {
            dependency
            for spec in specs
            for dependency in spec["pixi_host_dependencies"]
        }
    )
    extra_run = sorted(
        {
            dependency
            for spec in specs
            for dependency in spec["pixi_run_dependencies"]
        }
    )
    mojo_requirement = json.dumps(f"mojo-compiler {mojo_constraint}")
    lines.extend(
        [
            "",
            "requirements:",
            "  build:",
            "    - ${{ compiler('rust') }}",
            "    - ${{ compiler('c') }}",
            "    - ${{ stdlib('c') }}",
            f"    - {mojo_requirement}",
        ]
    )
    lines.extend(f"    - {json.dumps(dependency)}" for dependency in extra_build)
    lines.extend(
        [
            "  host:",
            f"    - {mojo_requirement}",
        ]
    )
    lines.extend(f"    - {json.dumps(dependency)}" for dependency in extra_host)
    lines.extend(
        [
            "  run:",
            f"    - {mojo_requirement}",
        ]
    )
    lines.extend(f"    - {json.dumps(dependency)}" for dependency in extra_run)
    lines.extend(
        [
            "",
            "tests:",
            "  - package_contents:",
            "      files:",
        ]
    )
    for spec in sorted(specs, key=lambda item: item["id"]):
        lines.extend(
            [
                f"        - lib/mojo/{spec['mojo_package']}.mojoc",
                "        - if: linux",
                "          then:",
                f"            - lib/lib{spec['ffi_crate']}.so",
                "        - if: osx",
                "          then:",
                f"            - lib/lib{spec['ffi_crate']}.dylib",
            ]
        )
    all_tests = [
        (spec["id"], test)
        for spec in sorted(specs, key=lambda item: item["id"])
        for test in spec["tests"]
    ]
    if all_tests:
        lines.extend(["  - script:"])
        for binding_id, test in all_tests:
            lines.append(f"      - mojo run {binding_id}/{test}")
        lines.extend(["    files:", "      source:"])
        for binding_id, test in all_tests:
            lines.append(f"        - {binding_id}/{test}")
    lines.extend(
        [
            "",
            "about:",
            "  summary: Generated Rust-to-Mojo bindings for the parent project.",
            "",
        ]
    )
    return "\n".join(lines)


def aggregate_content_hash(specs: list[dict[str, Any]]) -> str:
    """Hash every packaged binding input without host paths or mtimes."""

    digest = hashlib.sha256()
    digest.update(
        f"{GENERATOR_NAME}\0{GENERATOR_VERSION}\0{GENERATOR_SHA256}\0".encode(
            "utf-8"
        )
    )
    for spec in sorted(specs, key=lambda item: item["id"]):
        content_files = spec.get("content_files")
        content_modes = spec.get("content_modes")
        if isinstance(content_files, dict):
            items = sorted(content_files.items())
            if not isinstance(content_modes, dict) or set(content_modes) != set(
                content_files
            ):
                raise IntegrationError(
                    f"Binding {spec['id']!r} has incomplete content modes"
                )
        else:
            paths = list(iter_binding_files(spec["root"], spec["cargo_manifest"]))
            items = [
                (path.relative_to(spec["root"]).as_posix(), path.read_bytes())
                for path in paths
            ]
            content_modes = {
                path.relative_to(spec["root"]).as_posix(): normalized_mode(path)
                for path in paths
            }
        for relative, content in items:
            digest.update(spec["id"].encode("utf-8"))
            digest.update(b"\0")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(content_modes[relative]).encode("ascii"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
    return digest.hexdigest()[:12]


def validate_closed_aggregate_inventory(
    aggregate_root: Path,
    provenance: dict[str, Any] | None,
    previous_hashes: dict[str, str],
) -> list[Path]:
    """Validate the aggregate root's deliberately tiny build context."""

    if not aggregate_root.exists():
        return []
    if aggregate_root.is_symlink() or not aggregate_root.is_dir():
        raise IntegrationError(
            f"Aggregate binding root must be a real directory: {aggregate_root}"
        )
    if any("/" in relative for relative in previous_hashes):
        raise IntegrationError(
            "Aggregate provenance may own only root-level generated files"
        )
    expected_files = set(previous_hashes)
    if provenance is not None:
        expected_files.add(_AGGREGATE_MANIFEST)
    actual_files: set[str] = set()
    binding_directories: list[Path] = []
    for child in sorted(aggregate_root.iterdir()):
        if child.is_symlink():
            raise IntegrationError(
                f"Aggregate binding entries may not be symlinks: {child}"
            )
        if child.is_file():
            actual_files.add(child.name)
        elif child.is_dir():
            if not (child / "binding.toml").is_file():
                raise IntegrationError(
                    f"Unknown directory in closed aggregate build context: {child}"
                )
            binding_directories.append(child)
        else:
            raise IntegrationError(
                f"Unknown entry in closed aggregate build context: {child}"
            )
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        details: list[str] = []
        if unexpected:
            details.append("untracked files: " + ", ".join(unexpected))
        if missing:
            details.append("missing tracked files: " + ", ".join(missing))
        raise IntegrationError(
            "Aggregate root is not a closed generated tree ("
            + "; ".join(details)
            + ")"
        )
    folded: dict[str, str] = {}
    for child in binding_directories:
        previous = folded.get(child.name.casefold())
        if previous is not None:
            raise IntegrationError(
                f"Installed binding directories {previous!r} and {child.name!r} "
                "collide on case-insensitive filesystems"
            )
        folded[child.name.casefold()] = child.name
    if provenance is not None:
        binding_ids = provenance.get("bindings")
        actual_ids = sorted(child.name for child in binding_directories)
        if (
            not isinstance(binding_ids, list)
            or not all(isinstance(item, str) for item in binding_ids)
            or binding_ids != sorted(set(binding_ids))
            or binding_ids != actual_ids
        ):
            raise IntegrationError(
                "Aggregate provenance binding inventory does not match installed directories"
            )
    elif binding_directories:
        raise IntegrationError(
            "Installed binding directories require aggregate manifest.json provenance"
        )
    return binding_directories


def _provenance_identity(spec: dict[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "name": spec["crate_name"],
        "version": spec["crate_version"],
        "source_kind": spec["source_kind"],
        "features": spec["crate_features"],
        "default_features": spec["default_features"],
    }
    if spec["source_kind"] == "registry":
        identity["checksum"] = spec["crate_checksum"]
    else:
        identity["git"] = spec["crate_git"]
        identity["rev"] = spec["crate_rev"]
    return identity


def _validate_binding_provenance_identity(
    provenance: dict[str, Any], spec: dict[str, Any], path: Path
) -> None:
    expected_ffi = {
        "crate_name": spec["ffi_crate"],
        "cargo_manifest": spec["cargo_manifest"],
        "cargo_lock": spec["cargo_lock"],
        "tests": spec["ffi_tests"],
    }
    expected_mojo = {
        "source_dir": spec["mojo_source"],
        "tests": spec["tests"],
    }
    if (
        provenance.get("crate") != _provenance_identity(spec)
        or provenance.get("tools") != spec["tools"]
        or provenance.get("ffi") != expected_ffi
        or provenance.get("mojo") != expected_mojo
    ):
        raise IntegrationError(
            f"Existing {path} source request or build layout no longer matches binding.toml"
        )


def discover_binding_specs(
    aggregate_root: Path,
    binding_directories: list[Path],
    staged_spec: dict[str, Any],
) -> list[dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {staged_spec["id"]: staged_spec}
    if aggregate_root.is_dir():
        for child in binding_directories:
            if child.name == staged_spec["id"]:
                continue
            spec = validate_binding_root(child)
            if spec["id"] != child.name:
                raise IntegrationError(
                    f"Installed binding directory {child.name!r} contains id "
                    f"{spec['id']!r}; directory and manifest must match"
                )
            if spec["id"] in specs:
                raise IntegrationError(f"Duplicate installed binding id: {spec['id']}")
            provenance_path = child / _AGGREGATE_MANIFEST
            provenance = _load_json_manifest(
                provenance_path, "installed binding provenance"
            )
            if provenance is None:
                raise IntegrationError(
                    f"Installed binding {spec['id']!r} has no {provenance_path}"
                )
            validate_provenance_manifest(
                provenance, provenance_path, binding_id=spec["id"]
            )
            _validate_binding_provenance_identity(provenance, spec, provenance_path)
            canonical_resolution = canonicalize_resolution(
                provenance.get("resolution"), spec, aggregate_root.parent.parent
            )
            if canonical_resolution is None or canonical_resolution != provenance.get(
                "resolution"
            ):
                raise IntegrationError(
                    f"Installed binding {spec['id']!r} lacks canonical source resolution"
                )
            hashes = _previous_hashes(provenance, provenance_path)
            modes = _previous_modes(provenance, provenance_path, hashes)
            ensure_closed_binding_inventory(
                child,
                hashes,
                True,
                label=f"installed binding {spec['id']!r}",
            )
            ensure_destinations_are_not_symlinks(child, hashes)
            ensure_tracked_files_unmodified(
                child,
                hashes,
                modes,
                False,
                label=f"installed binding {spec['id']!r}",
            )
            spec["content_files"] = {
                relative: child.joinpath(*PurePosixPath(relative).parts).read_bytes()
                for relative in hashes
            }
            spec["content_modes"] = modes
            specs[spec["id"]] = spec
    mojo_packages: dict[str, str] = {}
    ffi_crates: dict[str, str] = {}
    for spec in specs.values():
        for value, owner, label in (
            (spec["mojo_package"], mojo_packages, "Mojo package"),
            (spec["ffi_crate"], ffi_crates, "FFI crate"),
        ):
            collision_key = value.casefold()
            previous = owner.get(collision_key)
            if previous is not None and previous != spec["id"]:
                raise IntegrationError(
                    f"{label} name {value!r} is shared by bindings "
                    f"{previous!r} and {spec['id']!r}"
                )
            owner[collision_key] = spec["id"]

    symbol_prefixes: dict[str, str] = {}
    loader_environments: dict[str, str] = {}
    namespace_collisions: list[str] = []
    for spec in sorted(specs.values(), key=lambda item: item["id"]):
        for value, owner, label in (
            (
                _binding_symbol_prefix(spec["id"]),
                symbol_prefixes,
                "C symbol prefix",
            ),
            (
                _binding_loader_environment(spec["id"]),
                loader_environments,
                "loader environment variable",
            ),
        ):
            previous = owner.get(value)
            if previous is not None and previous != spec["id"]:
                namespace_collisions.append(
                    f"{label} {value!r} is shared by bindings "
                    f"{previous!r} and {spec['id']!r}"
                )
            else:
                owner[value] = spec["id"]
    if namespace_collisions:
        raise IntegrationError(
            "Normalized binding namespaces collide: "
            + "; ".join(namespace_collisions)
        )
    return sorted(specs.values(), key=lambda item: item["id"])


def _aggregate_provenance(
    package_name: str,
    version: str,
    mojo_constraint: str,
    artifact_mode: str,
    root_publish_owned: bool,
    specs: list[dict[str, Any]],
    files: dict[str, bytes],
    modes: dict[str, int],
) -> bytes:
    payload = {
        "schema_version": 1,
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
            "sha256": GENERATOR_SHA256,
        },
        "package": {
            "name": package_name,
            "version": version,
            "mojo_constraint": mojo_constraint,
            "artifact_mode": artifact_mode,
        },
        "root_manifest": {"publish_owned": root_publish_owned},
        "bindings": [spec["id"] for spec in specs],
        "generated_files": {
            relative: sha256_bytes(content)
            for relative, content in sorted(files.items())
        },
        "generated_modes": dict(sorted(modes.items())),
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def desired_aggregate_files(
    package_name: str,
    version: str,
    specs: list[dict[str, Any]],
    mojo_constraint: str,
    artifact_mode: str,
    root_publish_owned: bool,
) -> tuple[dict[str, bytes], dict[str, int]]:
    files = {
        "pixi.toml": render_nested_manifest(
            package_name, version, artifact_mode
        ).encode("utf-8"),
        "recipe.yaml": render_recipe(
            package_name, version, specs, mojo_constraint
        ).encode("utf-8"),
    }
    modes = {relative: 0o644 for relative in files}
    files[_AGGREGATE_MANIFEST] = _aggregate_provenance(
        package_name,
        version,
        mojo_constraint,
        artifact_mode,
        root_publish_owned,
        specs,
        files,
        modes,
    )
    modes[_AGGREGATE_MANIFEST] = 0o644
    return files, modes


def _assert_unmanaged_files_safe(
    root: Path,
    desired: dict[str, bytes],
    previous_hashes: dict[str, str],
    force: bool,
    label: str,
) -> None:
    structural = [
        relative
        for relative in desired
        if (root / relative).exists()
        and ((root / relative).is_symlink() or (root / relative).is_dir())
    ]
    if structural:
        raise IntegrationError(
            f"Refusing structurally unsafe {label} destinations: "
            + ", ".join(sorted(structural))
        )
    conflicts = [] if force else [
        relative
        for relative, content in desired.items()
        if relative != _AGGREGATE_MANIFEST
        and relative not in previous_hashes
        and (root / relative).exists()
        and (root / relative).read_bytes() != content
    ]
    if conflicts:
        raise IntegrationError(
            f"Refusing to overwrite untracked {label} files: "
            + ", ".join(sorted(conflicts))
            + ". Pass --force-generated only if those files may be replaced."
        )


def ensure_destinations_are_not_symlinks(
    root: Path, relative_paths: Iterable[str]
) -> None:
    """Reject existing symlink components before any destination read or write."""

    for relative in relative_paths:
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                raise IntegrationError(
                    f"Generated destination contains a symlink component: {current}"
                )


def write_if_changed(path: Path, content: bytes, mode: int = 0o644) -> bool:
    if (
        path.exists()
        and path.is_file()
        and path.read_bytes() == content
        and normalized_mode(path) == mode
    ):
        return False
    if path.exists() and path.is_dir():
        raise IntegrationError(f"Cannot replace directory with generated file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    temporary.chmod(mode)
    os.replace(temporary, path)
    return True


def _remove_empty_parents(path: Path, stop: Path) -> None:
    parent = path.parent
    while parent != stop and parent != parent.parent:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _changes(
    project: Path,
    desired: dict[str, bytes],
    desired_modes: dict[str, int],
    stale: Iterable[str],
) -> tuple[list[str], list[str]]:
    changed = sorted(
        relative
        for relative, content in desired.items()
        if not (project / relative).is_file()
        or (project / relative).read_bytes() != content
        or normalized_mode(project / relative) != desired_modes[relative]
    )
    removed = sorted(relative for relative in stale if (project / relative).exists())
    return changed, removed


def run_pixi_install(project: Path, pixi: str) -> None:
    executable = shutil.which(pixi) if os.sep not in pixi else pixi
    if not executable:
        raise IntegrationError(f"Cannot find Pixi executable {pixi!r}")
    subprocess.run(
        [executable, "install", "--manifest-path", str(project / "pixi.toml")],
        cwd=project,
        check=True,
    )


def integrate(args: argparse.Namespace) -> dict[str, Any]:
    project = args.project.resolve()
    project_info = validate_project(project, args.artifact_mode)
    staged_spec = validate_binding_root(args.binding_root)
    if staged_spec["tools"]["mojo_version"] != project_info["mojo_version"]:
        raise IntegrationError(
            "The target project's exact Mojo compiler version "
            f"{project_info['mojo_version']!r} does not match binding "
            f"[tools].mojo_version {staged_spec['tools']['mojo_version']!r}. "
            "Regenerate the binding for the project compiler or align the project "
            "Mojo requirement."
        )
    aggregate_root = project / "vendor" / "rust-bindings"
    binding_destination = aggregate_root / staged_spec["id"]
    ensure_destinations_are_not_symlinks(
        project,
        (
            "pixi.toml",
            "vendor",
            "vendor/rust-bindings",
            f"vendor/rust-bindings/{staged_spec['id']}",
        ),
    )

    aggregate_provenance_path = aggregate_root / _AGGREGATE_MANIFEST
    prior_aggregate_manifest = _load_json_manifest(
        aggregate_provenance_path, "aggregate provenance"
    )
    validate_provenance_manifest(
        prior_aggregate_manifest, aggregate_provenance_path
    )
    prior_aggregate_hashes = _previous_hashes(
        prior_aggregate_manifest, aggregate_provenance_path
    )
    prior_aggregate_modes = _previous_modes(
        prior_aggregate_manifest,
        aggregate_provenance_path,
        prior_aggregate_hashes,
    )
    binding_directories = validate_closed_aggregate_inventory(
        aggregate_root, prior_aggregate_manifest, prior_aggregate_hashes
    )
    ensure_destinations_are_not_symlinks(
        aggregate_root, prior_aggregate_hashes
    )
    ensure_tracked_files_unmodified(
        aggregate_root,
        prior_aggregate_hashes,
        prior_aggregate_modes,
        args.force_generated,
        label="aggregate package",
    )

    prior_binding_manifest = _load_json_manifest(
        binding_destination / _AGGREGATE_MANIFEST, "binding provenance"
    )
    validate_provenance_manifest(
        prior_binding_manifest,
        binding_destination / _AGGREGATE_MANIFEST,
        binding_id=staged_spec["id"],
    )
    prior_binding_hashes = _previous_hashes(
        prior_binding_manifest, binding_destination / _AGGREGATE_MANIFEST
    )
    prior_binding_modes = _previous_modes(
        prior_binding_manifest,
        binding_destination / _AGGREGATE_MANIFEST,
        prior_binding_hashes,
    )
    ensure_closed_binding_inventory(
        binding_destination,
        prior_binding_hashes,
        prior_binding_manifest is not None,
        label=f"binding {staged_spec['id']!r}",
    )
    ensure_destinations_are_not_symlinks(
        binding_destination, prior_binding_hashes
    )
    ensure_tracked_files_unmodified(
        binding_destination,
        prior_binding_hashes,
        prior_binding_modes,
        args.force_generated,
        label=f"binding {staged_spec['id']!r}",
    )
    if prior_binding_manifest is not None:
        _validate_binding_provenance_identity(
            prior_binding_manifest,
            staged_spec,
            binding_destination / _AGGREGATE_MANIFEST,
        )

    requested_resolution = parse_resolution(args.resolution)
    reused_resolution = False
    if requested_resolution is None and prior_binding_manifest is not None:
        requested_resolution = prior_binding_manifest.get("resolution")
        reused_resolution = True
    if requested_resolution is None:
        raise IntegrationError(
            "First integration requires --resolution with schema-v1 resolver JSON; "
            "regeneration may reuse canonical provenance"
        )
    canonical_resolution = canonicalize_resolution(
        requested_resolution, staged_spec, project
    )
    if reused_resolution and canonical_resolution != requested_resolution:
        raise IntegrationError(
            "Existing binding provenance is not canonical; pass fresh --resolution JSON"
        )
    requested_resolution = canonical_resolution
    binding_files, binding_modes = desired_binding_files(
        staged_spec, requested_resolution
    )
    ensure_destinations_are_not_symlinks(binding_destination, binding_files)
    _assert_unmanaged_files_safe(
        binding_destination,
        binding_files,
        prior_binding_hashes,
        args.force_generated,
        f"binding {staged_spec['id']!r}",
    )
    packaged_content = {
        relative: content
        for relative, content in binding_files.items()
        if relative != _AGGREGATE_MANIFEST
    }
    staged_spec["content_files"] = packaged_content
    staged_spec["content_modes"] = {
        relative: mode
        for relative, mode in binding_modes.items()
        if relative != _AGGREGATE_MANIFEST
    }

    specs = discover_binding_specs(
        aggregate_root, binding_directories, staged_spec
    )
    for spec in specs:
        if spec["tools"]["mojo_version"] != project_info["mojo_version"]:
            raise IntegrationError(
                f"Installed binding {spec['id']!r} records [tools].mojo_version "
                f"{spec['tools']['mojo_version']!r}, which does not match the target "
                f"project's exact Mojo compiler version {project_info['mojo_version']!r}."
            )
    aggregate_package = normalized_package_name(
        f"{project_info['package_name']}-rust-mojo-bindings"
    )
    prior_publish_owned = False
    if prior_aggregate_manifest is not None:
        prior_root_manifest = prior_aggregate_manifest.get("root_manifest")
        if prior_root_manifest is not None:
            if not isinstance(prior_root_manifest, dict) or type(
                prior_root_manifest.get("publish_owned")
            ) is not bool:
                raise IntegrationError(
                    "Existing aggregate provenance has invalid root_manifest ownership"
                )
            prior_publish_owned = prior_root_manifest["publish_owned"]

    root_path = project / "pixi.toml"
    patched_root, warnings, root_publish_owned = patch_root_manifest(
        root_path.read_text(encoding="utf-8"),
        aggregate_package,
        project_info["artifact_mode"],
        prior_publish_owned,
    )
    aggregate_files, aggregate_modes = desired_aggregate_files(
        aggregate_package,
        project_info["package_version"],
        specs,
        project_info["mojo_constraint"],
        project_info["artifact_mode"],
        root_publish_owned,
    )
    if prior_aggregate_manifest is not None:
        prior_package = prior_aggregate_manifest.get("package")
        if (
            isinstance(prior_package, dict)
            and prior_package.get("name") != aggregate_package
        ):
            raise IntegrationError(
                "The root package name changed, which requires an explicit "
                "aggregate dependency migration"
            )
    ensure_destinations_are_not_symlinks(aggregate_root, aggregate_files)
    _assert_unmanaged_files_safe(
        aggregate_root,
        aggregate_files,
        prior_aggregate_hashes,
        args.force_generated,
        "aggregate package",
    )

    desired_project_files: dict[str, bytes] = {}
    desired_project_modes: dict[str, int] = {}
    binding_prefix = Path("vendor/rust-bindings") / staged_spec["id"]
    for relative, content in binding_files.items():
        project_relative = (binding_prefix / relative).as_posix()
        desired_project_files[project_relative] = content
        desired_project_modes[project_relative] = binding_modes[relative]
    for relative, content in aggregate_files.items():
        project_relative = (Path("vendor/rust-bindings") / relative).as_posix()
        desired_project_files[project_relative] = content
        desired_project_modes[project_relative] = aggregate_modes[relative]
    desired_project_files["pixi.toml"] = patched_root.encode("utf-8")
    desired_project_modes["pixi.toml"] = normalized_mode(root_path)

    stale_binding = {
        (binding_prefix / relative).as_posix()
        for relative in set(prior_binding_hashes) - set(binding_files)
    }
    stale_aggregate = {
        (Path("vendor/rust-bindings") / relative).as_posix()
        for relative in set(prior_aggregate_hashes) - set(aggregate_files)
    }
    stale = stale_binding | stale_aggregate
    changed, removed = _changes(
        project, desired_project_files, desired_project_modes, stale
    )
    if not args.check:
        provenance_paths = {
            relative
            for relative in desired_project_files
            if relative.endswith("/manifest.json")
        }
        ordinary_paths = (
            set(desired_project_files) - provenance_paths - {"pixi.toml"}
        )
        for relative in sorted(ordinary_paths):
            write_if_changed(
                project / relative,
                desired_project_files[relative],
                desired_project_modes[relative],
            )
        for relative in sorted(stale):
            candidate = project / relative
            if candidate.is_symlink() or candidate.is_dir():
                raise IntegrationError(
                    f"Refusing unsafe removal of stale generated path: {candidate}"
                )
            if candidate.is_file():
                candidate.unlink()
                stop = (
                    binding_destination
                    if relative in stale_binding
                    else aggregate_root
                )
                _remove_empty_parents(candidate, stop)
        # Provenance describes the completed copy and is committed only after
        # stale owned files are gone. The narrowly patched user manifest is last.
        for relative in sorted(provenance_paths):
            write_if_changed(
                project / relative,
                desired_project_files[relative],
                desired_project_modes[relative],
            )
        write_if_changed(
            project / "pixi.toml",
            desired_project_files["pixi.toml"],
            desired_project_modes["pixi.toml"],
        )
        final_binding_hashes = {
            relative: sha256_bytes(content)
            for relative, content in binding_files.items()
            if relative != _AGGREGATE_MANIFEST
        }
        ensure_closed_binding_inventory(
            binding_destination,
            final_binding_hashes,
            True,
            label=f"binding {staged_spec['id']!r}",
        )
        validate_closed_aggregate_inventory(
            aggregate_root,
            json.loads(aggregate_files[_AGGREGATE_MANIFEST]),
            {
                relative: sha256_bytes(content)
                for relative, content in aggregate_files.items()
                if relative != _AGGREGATE_MANIFEST
            },
        )

    artifact_commands = (
        ["pixi publish --target-dir <directory>"]
        if project_info["artifact_mode"] == "publish"
        else [
            "pixi build --manifest-path vendor/rust-bindings/pixi.toml "
            "--output-dir <directory>",
            "pixi build --output-dir <directory>",
        ]
    )
    report = {
        "binding": {
            "id": staged_spec["id"],
            "crate": staged_spec["crate_name"],
            "version": staged_spec["crate_version"],
            "mojo_package": staged_spec["mojo_package"],
        },
        "aggregate_package": aggregate_package,
        "artifact_mode": project_info["artifact_mode"],
        "artifact_mode_source": project_info["artifact_mode_source"],
        "artifact_commands": artifact_commands,
        "installed_bindings": [spec["id"] for spec in specs],
        "changed": changed,
        "removed": removed,
        "warnings": warnings,
        "mode": "check" if args.check else "write",
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--binding-root",
        type=Path,
        required=True,
        help="Complete agent-generated binding staging tree",
    )
    parser.add_argument(
        "--resolution",
        help="Resolution provenance as a JSON object, JSON file, or @JSON-file",
    )
    parser.add_argument(
        "--artifact-mode",
        choices=("build", "publish"),
        help=(
            "Pixi artifact workflow to generate: 'build' omits publish metadata "
            "for Pixi 0.59 compatibility; 'publish' enables the Pixi 0.76+ "
            "multi-package publish workflow. When omitted, requires-pixi >=0.76 "
            "selects publish and all other/ambiguous constraints select build."
        ),
    )
    parser.add_argument("--install", action="store_true", help="Run pixi install")
    parser.add_argument("--pixi", default="pixi", help="Pixi executable for --install")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 if integration would change checked-in files",
    )
    parser.add_argument(
        "--force-generated",
        action="store_true",
        help="Replace files previously owned by generated provenance",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = integrate(args)
        print(json.dumps(report, indent=2, sort_keys=True))
        if args.check and (report["changed"] or report["removed"]):
            return 1
        if args.install:
            if args.check:
                raise IntegrationError("--install cannot be combined with --check")
            run_pixi_install(args.project.resolve(), args.pixi)
        return 0
    except (IntegrationError, OSError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
