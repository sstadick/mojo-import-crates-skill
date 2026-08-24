#!/usr/bin/env python3
"""Resolve an exact Rust crate request with Cargo and emit deterministic JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Iterable
from urllib.parse import urlsplit


REGISTRY_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_][A-Za-z0-9_-]*)@(?P<version>[0-9][^@]*)$"
)
GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
IGNORED_ROOT_DIRECTORIES = {".git", ".pixi", "target"}


class ResolveError(RuntimeError):
    """A crate request that cannot be resolved exactly and safely."""


def toml_string(value: str) -> str:
    # JSON strings are valid TOML basic strings for the characters accepted by
    # crate names, versions, URLs, revisions, features, and absolute paths.
    return json.dumps(value, ensure_ascii=False)


def parse_registry_spec(spec: str) -> tuple[str, str]:
    match = REGISTRY_SPEC_RE.fullmatch(spec)
    if not match:
        raise ResolveError(
            "A crates.io request must be NAME@VERSION, for example "
            "example-crate@1.2.3"
        )
    version = match.group("version")
    if any(token in version for token in ("*", "^", "~", ">", "<", ",", "=")):
        raise ResolveError(
            f"The registry version must be concrete, not a range: {version!r}"
        )
    return match.group("name"), version


def validate_git_revision(revision: str) -> str:
    if not GIT_COMMIT_RE.fullmatch(revision):
        raise ResolveError(
            "A Git crate --rev must be a full 40-character commit hash; "
            "branch and tag names are mutable"
        )
    return revision.lower()


def validate_git_url(url: str) -> str:
    """Reject local or credential-bearing Git locations before provenance output."""

    if not url or url != url.strip() or any(character in url for character in "\r\n\0"):
        raise ResolveError("A Git crate URL must be a non-empty, single-line URL")
    if url.startswith(("/", "./", "../", "~")) or url.startswith("file:"):
        raise ResolveError(
            "A Git crate must use a remote URL; use --path for local source"
        )

    if "://" in url:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "ssh", "git"} or not parsed.hostname:
            raise ResolveError(
                "A Git crate URL must use https, ssh, or git with a remote host"
            )
        if parsed.password is not None or (
            parsed.scheme == "https" and parsed.username is not None
        ):
            raise ResolveError(
                "Do not put credentials in a Git URL; use a credential helper"
            )
        if parsed.query or parsed.fragment:
            raise ResolveError(
                "Git URL queries and fragments are not persisted; pass the commit with --rev"
            )
        return url

    # Cargo also accepts SCP-like SSH locations such as git@github.com:org/repo.
    if re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:.+", url):
        return url
    raise ResolveError(
        "A Git crate URL must be an https, ssh, git, or SCP-like remote URL"
    )


def package_name_from_manifest(path: Path) -> str:
    manifest_path = path / "Cargo.toml" if path.is_dir() else path
    try:
        parsed = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ResolveError(f"Missing local Cargo manifest: {manifest_path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ResolveError(f"Invalid local Cargo manifest {manifest_path}: {error}") from error
    package = parsed.get("package")
    if not isinstance(package, dict) or not isinstance(package.get("name"), str):
        raise ResolveError(
            "A workspace-only local Cargo.toml needs an explicit --package selection"
        )
    return package["name"]


def dependency_table(
    *,
    package_name: str,
    features: list[str],
    version: str | None = None,
    path: Path | None = None,
    git: str | None = None,
    rev: str | None = None,
    default_features: bool = True,
) -> str:
    fields = [f"package = {toml_string(package_name)}"]
    if version is not None:
        fields.append(f"version = {toml_string('=' + version)}")
    if path is not None:
        fields.append(f"path = {toml_string(str(path.resolve()))}")
    if git is not None:
        fields.append(f"git = {toml_string(git)}")
        fields.append(f"rev = {toml_string(rev or '')}")
    if not default_features:
        fields.append("default-features = false")
    if features:
        rendered = ", ".join(toml_string(feature) for feature in sorted(set(features)))
        fields.append(f"features = [{rendered}]")
    return "binding_target = { " + ", ".join(fields) + " }"


def local_package_directory(
    cargo: str,
    root: Path,
    package_name: str,
    offline: bool,
) -> Path:
    manifest_path = root / "Cargo.toml"
    command = [
        cargo,
        "metadata",
        "--format-version",
        "1",
        "--no-deps",
        "--manifest-path",
        str(manifest_path),
    ]
    if offline:
        command.append("--offline")
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        metadata = json.loads(completed.stdout)
    except FileNotFoundError as error:
        raise ResolveError(f"Cannot find Cargo executable {cargo!r}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise ResolveError(f"Cargo metadata failed for local path: {detail}") from error
    except json.JSONDecodeError as error:
        raise ResolveError(f"Cargo returned invalid local metadata JSON: {error}") from error

    matches = [
        package
        for package in metadata.get("packages", [])
        if isinstance(package, dict) and package.get("name") == package_name
    ]
    if len(matches) != 1:
        raise ResolveError(
            f"Local path must resolve exactly one package named {package_name!r}; "
            f"found {len(matches)}"
        )
    selected_manifest = matches[0].get("manifest_path")
    if not isinstance(selected_manifest, str):
        raise ResolveError(f"Cargo metadata omitted the manifest for {package_name!r}")
    return Path(selected_manifest).resolve().parent


def resolver_manifest(dependency: str) -> str:
    return (
        "[package]\n"
        'name = "rust_mojo_resolution_probe"\n'
        'version = "0.0.0"\n'
        'edition = "2021"\n'
        'publish = false\n\n'
        "[dependencies]\n"
        f"{dependency}\n"
    )


def cargo_metadata(
    cargo: str, manifest_path: Path, offline: bool
) -> tuple[dict[str, Any], str]:
    command = [
        cargo,
        "metadata",
        "--format-version",
        "1",
        "--manifest-path",
        str(manifest_path),
    ]
    if offline:
        command.append("--offline")
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        version = subprocess.run(
            [cargo, "--version"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except FileNotFoundError as error:
        raise ResolveError(f"Cannot find Cargo executable {cargo!r}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise ResolveError(f"Cargo metadata failed: {detail}") from error
    try:
        return json.loads(completed.stdout), version
    except json.JSONDecodeError as error:
        raise ResolveError(f"Cargo returned invalid metadata JSON: {error}") from error


def selected_dependency_id(metadata: dict[str, Any]) -> str:
    resolve = metadata.get("resolve")
    root_id = resolve.get("root") if isinstance(resolve, dict) else None
    nodes = resolve.get("nodes", []) if isinstance(resolve, dict) else []
    root_node = next(
        (node for node in nodes if isinstance(node, dict) and node.get("id") == root_id),
        None,
    )
    if not isinstance(root_node, dict):
        raise ResolveError("Cargo metadata did not identify the resolver probe root")
    dependencies = root_node.get("deps", [])
    matches = [
        dependency.get("pkg")
        for dependency in dependencies
        if isinstance(dependency, dict) and dependency.get("name") == "binding_target"
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise ResolveError("Cargo metadata did not resolve exactly one binding_target")
    return matches[0]


def lock_checksum(lock_path: Path, name: str, version: str, source: str | None) -> str | None:
    try:
        parsed = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return None
    for package in parsed.get("package", []):
        if (
            package.get("name") == name
            and package.get("version") == version
            and package.get("source") == source
        ):
            checksum = package.get("checksum")
            return checksum if isinstance(checksum, str) else None
    return None


def fingerprint_paths(root: Path) -> Iterable[Path]:
    ignored_roots = {root / name for name in IGNORED_ROOT_DIRECTORIES}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(path == ignored or ignored in path.parents for ignored in ignored_roots):
            continue
        if path.is_symlink():
            raise ResolveError(
                f"Local crate fingerprints do not follow symlinks: {relative.as_posix()}"
            )
        if not path.is_file():
            continue
        # Build scripts and Rust sources can consume arbitrary companion data
        # via include_bytes!/include_str! or external tools. Hash every source
        # tree file rather than guessing which suffixes affect the crate.
        yield path


def local_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    count = 0
    for path in fingerprint_paths(root):
        count += 1
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    if count == 0:
        raise ResolveError(f"No Rust source or Cargo files found below {root}")
    return digest.hexdigest()


def resolve(args: argparse.Namespace) -> dict[str, Any]:
    cargo = shutil.which(args.cargo) if "/" not in args.cargo else args.cargo
    if not cargo:
        raise ResolveError(f"Cannot find Cargo executable {args.cargo!r}")

    features = sorted(set(args.feature))
    if args.registry:
        package_name, requested_version = parse_registry_spec(args.registry)
        request: dict[str, Any] = {
            "kind": "registry",
            "name": package_name,
            "version": requested_version,
        }
        dependency = dependency_table(
            package_name=package_name,
            version=requested_version,
            features=features,
            default_features=not args.no_default_features,
        )
    elif args.path:
        if args.path.is_symlink():
            raise ResolveError(f"A local crate root may not be a symlink: {args.path}")
        local_path = args.path.resolve()
        if local_path.is_file() and local_path.name == "Cargo.toml":
            local_path = local_path.parent
        if not local_path.is_dir():
            raise ResolveError(
                f"A local crate path must be a package directory or Cargo.toml: {local_path}"
            )
        package_name = args.package or package_name_from_manifest(local_path)
        package_path = local_package_directory(
            cargo,
            local_path,
            package_name,
            args.offline,
        )
        requested_version = None
        request = {
            "kind": "path",
            "name": package_name,
            "path": str(local_path),
            "package_path": str(package_path),
            "fingerprint": local_fingerprint(local_path),
        }
        dependency = dependency_table(
            package_name=package_name,
            path=package_path,
            features=features,
            default_features=not args.no_default_features,
        )
    else:
        if not args.rev:
            raise ResolveError("A Git crate must use an immutable --rev")
        requested_commit = validate_git_revision(args.rev)
        git_url = validate_git_url(args.git)
        if not args.package:
            raise ResolveError("A Git crate needs --package NAME")
        package_name = args.package
        requested_version = None
        request = {
            "kind": "git",
            "name": package_name,
            "git": git_url,
            "rev": requested_commit,
        }
        dependency = dependency_table(
            package_name=package_name,
            git=git_url,
            rev=requested_commit,
            features=features,
            default_features=not args.no_default_features,
        )

    with tempfile.TemporaryDirectory(prefix="rust-mojo-resolve-") as directory:
        probe = Path(directory)
        (probe / "src").mkdir()
        (probe / "src" / "lib.rs").write_text(
            "// Cargo metadata resolution probe.\n", encoding="utf-8"
        )
        manifest_path = probe / "Cargo.toml"
        manifest_path.write_text(resolver_manifest(dependency), encoding="utf-8")
        metadata, cargo_version = cargo_metadata(cargo, manifest_path, args.offline)
        selected_id = selected_dependency_id(metadata)
        package = next(
            (
                candidate
                for candidate in metadata.get("packages", [])
                if candidate.get("id") == selected_id
            ),
            None,
        )
        if not isinstance(package, dict):
            raise ResolveError(f"Cargo metadata omitted selected package {selected_id}")
        if requested_version is not None and package.get("version") != requested_version:
            raise ResolveError(
                f"Cargo resolved {package.get('version')} instead of {requested_version}"
            )
        checksum = lock_checksum(
            probe / "Cargo.lock",
            package["name"],
            package["version"],
            package.get("source"),
        )
        if request["kind"] == "registry" and checksum is None:
            raise ResolveError(
                f"Cargo.lock omitted the registry checksum for "
                f"{package['name']} {package['version']}"
            )

        selected_node = next(
            (
                node
                for node in metadata.get("resolve", {}).get("nodes", [])
                if node.get("id") == selected_id
            ),
            {},
        )
        result = {
            "schema_version": 1,
            "request": {
                **request,
                "features": features,
                "default_features": not args.no_default_features,
            },
            "resolved": {
                "id": selected_id,
                "name": package["name"],
                "version": package["version"],
                "source": package.get("source"),
                "checksum": checksum,
                "manifest_path": package["manifest_path"],
                "enabled_features": sorted(selected_node.get("features", [])),
                "dependencies": sorted(
                    dependency["pkg"]
                    for dependency in selected_node.get("deps", [])
                    if isinstance(dependency, dict) and isinstance(dependency.get("pkg"), str)
                ),
            },
            "cargo_version": cargo_version,
        }
        if request["kind"] == "git":
            source = package.get("source")
            if not isinstance(source, str) or "#" not in source:
                raise ResolveError("Cargo did not report the resolved Git commit")
            resolved_commit = source.rsplit("#", 1)[1]
            if resolved_commit.lower() != requested_commit:
                raise ResolveError(
                    f"Cargo resolved Git commit {resolved_commit}, not {args.rev}"
                )
            result["resolved"]["git_commit"] = resolved_commit
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--registry", metavar="NAME@VERSION")
    source.add_argument("--path", type=Path)
    source.add_argument("--git")
    parser.add_argument("--rev", help="Immutable Git revision; required with --git")
    parser.add_argument("--package", help="Package name for Git/workspace path requests")
    parser.add_argument("--feature", action="append", default=[])
    parser.add_argument(
        "--no-default-features",
        action="store_true",
        help="Disable the crate's default Cargo features",
    )
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--offline", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(resolve(args), indent=2, sort_keys=True))
        return 0
    except ResolveError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
