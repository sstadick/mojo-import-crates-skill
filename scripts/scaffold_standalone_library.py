#!/usr/bin/env python3
"""Create the generic shell for a standalone Rust-backed Mojo library.

The scaffold is intentionally crate-agnostic. It creates only reusable project,
CI, downstream-consumer, and documentation infrastructure plus a minimal API
proof.
The binding workflow must replace those proofs with API-specific facade tests,
examples, README content, and guides before reporting completion.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

ASSET_ROOT = Path(__file__).resolve().parents[1] / "assets" / "standalone-library"
SUPPORTED_PLATFORMS = ("linux-64", "linux-aarch64", "osx-arm64")
RUNNERS = {
    "linux-64": "ubuntu-24.04",
    "linux-aarch64": "ubuntu-24.04-arm",
    "osx-arm64": "macos-15",
}
DEFAULT_SKILL_REPOSITORY = "https://github.com/sstadick/mojo-import-crates-skill"
_PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_RUST_PACKAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_MOJO_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SEMVER_RE = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)
_TOOL_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_GITHUB_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)"
)
_UNRESOLVED_TOKEN_RE = re.compile(r"@[A-Z][A-Z0-9_]*@")


class ScaffoldError(ValueError):
    """Raised when the requested standalone shell is unsafe or invalid."""


@dataclass(frozen=True)
class Config:
    project: Path
    package_name: str
    mojo_package: str
    package_version: str
    rust_crate: str
    rust_version: str
    mojo_version: str
    platforms: tuple[str, ...]
    authors: tuple[str, ...]
    github_repository: str | None
    default_branch: str
    skill_repository: str
    omit_ai_disclosure: bool


@dataclass(frozen=True)
class Asset:
    source: str
    destination: str
    executable: bool = False


ASSETS = (
    Asset("dot-gitignore", ".gitignore"),
    Asset("README.md.tmpl", "README.md"),
    Asset("CHANGELOG.md.tmpl", "CHANGELOG.md"),
    Asset("pixi.toml.tmpl", "pixi.toml"),
    Asset("package/__init__.mojo.tmpl", "@MOJO_PACKAGE@/__init__.mojo"),
    Asset("package/scaffold.mojo.tmpl", "@MOJO_PACKAGE@/scaffold.mojo"),
    Asset("tests/test_import.mojo.tmpl", "tests/test_import.mojo"),
    Asset("examples/import.mojo.tmpl", "examples/import.mojo"),
    Asset("scripts/check_generated.py", "scripts/check_generated.py", True),
    Asset("scripts/format_mojo.py", "scripts/format_mojo.py", True),
    Asset("ci/run_mojo_tests.py", "ci/run_mojo_tests.py", True),
    Asset("ci/run_examples.py", "ci/run_examples.py", True),
    Asset("ci/test_git_dependency.py.tmpl", "ci/test_git_dependency.py", True),
    Asset("ci/git-consumer/pixi.toml.in.tmpl", "ci/git-consumer/pixi.toml.in"),
    Asset(
        "ci/git-consumer/package/__init__.mojo.tmpl",
        "ci/git-consumer/@MOJO_PACKAGE@_ci_consumer/__init__.mojo",
    ),
    Asset(
        "ci/git-consumer/tests/test_consumer.mojo.tmpl",
        "ci/git-consumer/tests/test_consumer.mojo",
    ),
    Asset("github/workflows/ci.yml.tmpl", ".github/workflows/ci.yml"),
    Asset("github/workflows/docs.yml.tmpl", ".github/workflows/docs.yml"),
    Asset("modo.yaml.tmpl", "modo.yaml"),
    Asset("docs/dot-gitignore", "docs/.gitignore"),
    Asset("docs/src/_index.md.tmpl", "docs/src/_index.md"),
    Asset("docs/src/guide/_index.md.tmpl", "docs/src/guide/_index.md"),
    Asset("docs/site/hugo.yaml.tmpl", "docs/site/hugo.yaml"),
    Asset("docs/site/go.mod.tmpl", "docs/site/go.mod"),
    Asset("docs/site/go.sum", "docs/site/go.sum"),
    Asset("docs/site/assets/css/custom.css", "docs/site/assets/css/custom.css"),
    Asset(
        "docs/site/layouts/_default/_markup/render-codeblock.html",
        "docs/site/layouts/_default/_markup/render-codeblock.html",
    ),
    Asset(
        "docs/site/layouts/shortcodes/expand-all.html",
        "docs/site/layouts/shortcodes/expand-all.html",
    ),
    Asset(
        "docs/site/layouts/shortcodes/html.html",
        "docs/site/layouts/shortcodes/html.html",
    ),
    Asset("docs/templates/doctest.mojo", "docs/templates/doctest.mojo"),
    Asset("docs/templates/function.md", "docs/templates/function.md"),
    Asset("docs/templates/methods.md", "docs/templates/methods.md"),
    Asset("docs/templates/overload.md", "docs/templates/overload.md"),
    Asset(
        "docs/templates/signature_func.md",
        "docs/templates/signature_func.md",
    ),
)


def _validate_fullmatch(pattern: re.Pattern[str], value: str, label: str) -> str:
    if pattern.fullmatch(value) is None:
        raise ScaffoldError(f"Invalid {label}: {value!r}")
    return value


def validate_config(config: Config) -> None:
    _validate_fullmatch(_PACKAGE_RE, config.package_name, "Pixi package name")
    _validate_fullmatch(_MOJO_IDENTIFIER_RE, config.mojo_package, "Mojo package name")
    _validate_fullmatch(_SEMVER_RE, config.package_version, "package version")
    _validate_fullmatch(_RUST_PACKAGE_RE, config.rust_crate, "Rust package name")
    _validate_fullmatch(_SEMVER_RE, config.rust_version, "Rust package version")
    _validate_fullmatch(_TOOL_VERSION_RE, config.mojo_version, "Mojo version")
    _validate_fullmatch(_BRANCH_RE, config.default_branch, "default branch")
    if (
        ".." in config.default_branch
        or "//" in config.default_branch
        or "/." in config.default_branch
        or config.default_branch.endswith(("/", "."))
    ):
        raise ScaffoldError(f"Invalid default branch: {config.default_branch!r}")
    if not config.platforms:
        raise ScaffoldError("At least one target platform is required")
    unsupported = sorted(set(config.platforms) - set(SUPPORTED_PLATFORMS))
    if unsupported:
        raise ScaffoldError("Unsupported target platforms: " + ", ".join(unsupported))
    if len(set(config.platforms)) != len(config.platforms):
        raise ScaffoldError("Target platforms must not contain duplicates")
    if config.github_repository is not None:
        _validate_fullmatch(
            _GITHUB_RE, config.github_repository, "GitHub OWNER/REPOSITORY"
        )
    skill_url = urlsplit(config.skill_repository)
    if (
        skill_url.scheme != "https"
        or not skill_url.hostname
        or skill_url.username is not None
        or skill_url.password is not None
        or skill_url.query
        or skill_url.fragment
        or any(character.isspace() for character in config.skill_repository)
        or _UNRESOLVED_TOKEN_RE.search(config.skill_repository)
    ):
        raise ScaffoldError("Skill repository must be an HTTPS URL")
    if config.project == Path(config.project.anchor):
        raise ScaffoldError("Refusing to scaffold at a filesystem root")


def _yaml_matrix(platforms: Iterable[str]) -> str:
    return "\n".join(
        f"          - runner: {RUNNERS[platform]}\n            platform: {platform}"
        for platform in platforms
    )


def _github_values(config: Config) -> dict[str, str]:
    if config.github_repository is None:
        return {
            "BASE_URL": "http://localhost:1313/",
            "DOCS_DEPLOY_JOB": "",
            "DOCS_MODULE": f"standalone.local/{config.package_name}/docs",
            "HUGO_GITHUB_MENU": "",
            "MODO_SOURCE_URL": "",
            "README_DOC_LINK": "",
            "README_GIT_INSTALL": "",
        }

    match = _GITHUB_RE.fullmatch(config.github_repository)
    assert match is not None
    owner = match.group("owner")
    repository = match.group("repo")
    docs_url = f"https://{owner}.github.io/{repository}/"
    git_url = f"https://github.com/{owner}/{repository}.git"
    source_url = (
        f"https://github.com/{owner}/{repository}/blob/"
        f"{config.default_branch}/{config.mojo_package}"
    )
    deploy_job = f"""\n  deploy:
    name: Deploy documentation
    if: (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == 'refs/heads/{config.default_branch}'
    needs: build
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    permissions:
      contents: write

    steps:
      - name: Download generated site
        uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
        with:
          name: docs-site
          path: docs/site/public

      - name: Deploy to GitHub Pages
        uses: crazy-max/ghaction-github-pages@1d6ee9b181a81033a16bd707a1401afa978daab4 # v5.0.0
        with:
          target_branch: gh-pages
          build_dir: docs/site/public
          jekyll: false
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
"""
    return {
        "BASE_URL": docs_url,
        "DOCS_DEPLOY_JOB": deploy_job.rstrip(),
        "DOCS_MODULE": f"github.com/{owner}/{repository}/docs",
        "HUGO_GITHUB_MENU": f"""    - name: GitHub
      weight: 4
      url: "https://github.com/{owner}/{repository}"
      params:
        icon: github""",
        "MODO_SOURCE_URL": f"""source-url:
  {config.mojo_package}: {source_url}
""".rstrip(),
        "README_DOC_LINK": f"**[Documentation, guides, and examples]({docs_url})**",
        "README_GIT_INSTALL": f'''## Depend on `{config.package_name}` from Git

```sh
pixi workspace preview add pixi-build
pixi add --git "{git_url}" {config.package_name} && pixi install
```

Pin a full Git `rev` in `pixi.toml` for reproducible builds.''',
    }


def token_values(config: Config) -> dict[str, str]:
    authors = (
        f"authors = {json.dumps(list(config.authors))}\n" if config.authors else ""
    )
    disclosure = ""
    if not config.omit_ai_disclosure:
        disclosure = f"""> [!IMPORTANT]
> This is an AI-generated repository created with the
> [`bind-rust-to-mojo` skill]({config.skill_repository}/blob/main/SKILL.md)
> from [`mojo-import-crates-skill`]({config.skill_repository}). Review and test
> generated bindings before using them in critical applications."""
    values = {
        "AI_DISCLOSURE": disclosure,
        "AUTHORS": authors.rstrip(),
        "DEFAULT_BRANCH": config.default_branch,
        "MOJO_PACKAGE": config.mojo_package,
        "MOJO_VERSION": config.mojo_version,
        "PACKAGE_NAME": config.package_name,
        "PACKAGE_VERSION": config.package_version,
        "PLATFORMS": json.dumps(list(config.platforms)),
        "RUST_CRATE": config.rust_crate,
        "RUST_VERSION": config.rust_version,
        "SKILL_REPOSITORY": config.skill_repository,
        "WORKFLOW_MATRIX": _yaml_matrix(config.platforms),
    }
    values.update(_github_values(config))
    return values


def render(text: str, values: dict[str, str], label: str) -> str:
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    unresolved = sorted(set(_UNRESOLVED_TOKEN_RE.findall(text)))
    if unresolved:
        raise ScaffoldError(
            f"Asset {label} contains unresolved tokens: {', '.join(unresolved)}"
        )
    return text


def render_files(config: Config) -> dict[str, tuple[bytes, int]]:
    validate_config(config)
    values = token_values(config)
    files: dict[str, tuple[bytes, int]] = {}
    for asset in ASSETS:
        source = ASSET_ROOT / asset.source
        if not source.is_file():
            raise ScaffoldError(f"Missing standalone scaffold asset: {source}")
        destination = render(asset.destination, values, asset.destination)
        if destination in files:
            raise ScaffoldError(f"Duplicate scaffold destination: {destination}")
        content = render(source.read_text(encoding="utf-8"), values, asset.source)
        files[destination] = (
            content.encode("utf-8"),
            0o755 if asset.executable else 0o644,
        )
    return files


def _validate_target(project: Path) -> None:
    if project.is_symlink():
        raise ScaffoldError(f"Refusing symlink target: {project}")
    if project.exists() and not project.is_dir():
        raise ScaffoldError(f"Target exists and is not a directory: {project}")
    if project.exists():
        unexpected = sorted(
            entry.name for entry in project.iterdir() if entry.name != ".git"
        )
        if unexpected:
            raise ScaffoldError(
                "Standalone target must be empty except for .git; found: "
                + ", ".join(unexpected)
            )
    elif not project.parent.is_dir():
        raise ScaffoldError(f"Target parent does not exist: {project.parent}")


def scaffold(config: Config) -> list[str]:
    requested_project = config.project.expanduser()
    if requested_project.is_symlink():
        raise ScaffoldError(f"Refusing symlink target: {requested_project}")
    project = requested_project.resolve()
    config = replace(config, project=project)
    _validate_target(project)
    files = render_files(config)

    project.mkdir(exist_ok=True)
    for relative, (content, mode) in sorted(files.items()):
        destination = project / relative
        if destination.exists() or destination.is_symlink():
            raise ScaffoldError(
                f"Refusing existing scaffold destination: {destination}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        os.chmod(destination, mode)
    return sorted(files)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--package-name", required=True)
    parser.add_argument("--mojo-package", required=True)
    parser.add_argument("--package-version", default="0.1.0")
    parser.add_argument("--rust-crate", required=True)
    parser.add_argument("--rust-version", required=True)
    parser.add_argument("--mojo-version", required=True)
    parser.add_argument(
        "--platform",
        action="append",
        choices=SUPPORTED_PLATFORMS,
        dest="platforms",
    )
    parser.add_argument("--author", action="append", default=[], dest="authors")
    parser.add_argument("--github-repository")
    parser.add_argument("--default-branch", default="main")
    parser.add_argument("--skill-repository", default=DEFAULT_SKILL_REPOSITORY)
    parser.add_argument("--omit-ai-disclosure", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    return Config(
        project=args.project,
        package_name=args.package_name,
        mojo_package=args.mojo_package,
        package_version=args.package_version,
        rust_crate=args.rust_crate,
        rust_version=args.rust_version,
        mojo_version=args.mojo_version,
        platforms=tuple(args.platforms or SUPPORTED_PLATFORMS),
        authors=tuple(args.authors),
        github_repository=args.github_repository,
        default_branch=args.default_branch,
        skill_repository=args.skill_repository.rstrip("/"),
        omit_ai_disclosure=args.omit_ai_disclosure,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        created = scaffold(config_from_args(args))
    except (OSError, ScaffoldError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"project": str(args.project.expanduser().resolve()), "created": created},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
