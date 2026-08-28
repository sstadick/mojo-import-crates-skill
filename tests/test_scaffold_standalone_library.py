from __future__ import annotations

import importlib.util
import json
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tomllib

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "scaffold_standalone_library.py"
)
SPEC = importlib.util.spec_from_file_location("scaffold_standalone_library", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
scaffolder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scaffolder
SPEC.loader.exec_module(scaffolder)

INTEGRATOR_SCRIPT = SCRIPT.with_name("integrate_binding.py")
INTEGRATOR_SPEC = importlib.util.spec_from_file_location(
    "integrate_binding_for_scaffold_test", INTEGRATOR_SCRIPT
)
assert INTEGRATOR_SPEC is not None and INTEGRATOR_SPEC.loader is not None
integrator = importlib.util.module_from_spec(INTEGRATOR_SPEC)
INTEGRATOR_SPEC.loader.exec_module(integrator)


def config(project: Path, **overrides: object) -> object:
    values: dict[str, object] = {
        "project": project,
        "package_name": "fancy-bind",
        "mojo_package": "fancy_bind",
        "package_version": "0.1.0",
        "rust_crate": "fancy-crate",
        "rust_version": "2.3.4",
        "mojo_version": "1.0.0",
        "platforms": ("linux-64", "osx-arm64"),
        "authors": ("Ada Example <ada@example.com>",),
        "github_repository": "ExampleOrg/fancy-bind",
        "default_branch": "main",
        "skill_repository": scaffolder.DEFAULT_SKILL_REPOSITORY,
        "omit_ai_disclosure": False,
    }
    values.update(overrides)
    return scaffolder.Config(**values)


class ScaffoldStandaloneLibraryTests(unittest.TestCase):
    def test_github_scaffold_is_complete_deterministic_and_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            created = scaffolder.scaffold(config(project))

            asset_sources = {
                path.relative_to(scaffolder.ASSET_ROOT).as_posix()
                for path in scaffolder.ASSET_ROOT.rglob("*")
                if path.is_file()
            }
            self.assertEqual(
                asset_sources, {asset.source for asset in scaffolder.ASSETS}
            )
            self.assertEqual(created, sorted(created))
            self.assertEqual(len(created), len(scaffolder.ASSETS))
            self.assertTrue((project / "fancy_bind" / "__init__.mojo").is_file())
            self.assertTrue((project / "fancy_bind" / "scaffold.mojo").is_file())
            self.assertIn(
                "from .scaffold import import_ready",
                (project / "fancy_bind" / "__init__.mojo").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'doctest="scaffold_import_ready"',
                (project / "fancy_bind" / "scaffold.mojo").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "exports: true",
                (project / "modo.yaml").read_text(encoding="utf-8"),
            )
            self.assertTrue((project / ".github" / "workflows" / "ci.yml").is_file())

            manifest = tomllib.loads(
                (project / "pixi.toml").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["package"]["name"], "fancy-bind")
            self.assertEqual(
                manifest["package"]["build"]["config"]["pkg"]["path"], "fancy_bind"
            )
            self.assertEqual(
                manifest["workspace"]["platforms"], ["linux-64", "osx-arm64"]
            )
            self.assertEqual(
                manifest["workspace"]["authors"], ["Ada Example <ada@example.com>"]
            )
            self.assertEqual(manifest["dependencies"]["mojo"], "=1.0.0")

            project_info = integrator.validate_project(project, "publish")
            self.assertEqual(project_info["package_name"], "fancy-bind")
            self.assertEqual(project_info["mojo_version"], "1.0.0")
            self.assertEqual(project_info["artifact_mode"], "publish")

            readme = (project / "README.md").read_text(encoding="utf-8")
            self.assertIn("AI-generated repository", readme)
            self.assertIn("https://ExampleOrg.github.io/fancy-bind/", readme)
            self.assertIn(
                'pixi add --git "https://github.com/ExampleOrg/fancy-bind.git"', readme
            )
            self.assertIn("fancy-crate", readme)

            docs_workflow = (project / ".github/workflows/docs.yml").read_text(
                encoding="utf-8"
            )
            self.assertIn("deploy:", docs_workflow)
            self.assertIn("refs/heads/main", docs_workflow)
            self.assertIn("permissions:\n      contents: write", docs_workflow)

            ci_workflow = (project / ".github/workflows/ci.yml").read_text(
                encoding="utf-8"
            )
            self.assertIn("runner: ubuntu-24.04", ci_workflow)
            self.assertIn("runner: macos-15", ci_workflow)
            self.assertNotIn("ubuntu-24.04-arm", ci_workflow)

            for path in project.rglob("*"):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(
                    re.search(r"@[A-Z][A-Z0-9_]*@", text),
                    f"unresolved scaffold token in {path}",
                )
                self.assertNotIn("ziprs", text)

            for relative in (
                "scripts/check_generated.py",
                "scripts/format_mojo.py",
                "ci/run_mojo_tests.py",
                "ci/run_examples.py",
                "ci/test_git_dependency.py",
            ):
                path = project / relative
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
                subprocess.run(
                    [sys.executable, "-m", "py_compile", str(path)],
                    check=True,
                )

            git_consumer_help = subprocess.run(
                [sys.executable, str(project / "ci/test_git_dependency.py"), "--help"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("linux-aarch64", git_consumer_help)
            self.assertIn("--revision", git_consumer_help)

            with self.assertRaises(scaffolder.ScaffoldError):
                scaffolder.scaffold(config(project))

    def test_rejects_symlink_target_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            destination.mkdir()
            project = root / "project"
            project.symlink_to(destination, target_is_directory=True)

            with self.assertRaises(scaffolder.ScaffoldError):
                scaffolder.scaffold(config(project))
            self.assertEqual(list(destination.iterdir()), [])

    def test_local_scaffold_omits_hosted_links_deployment_and_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            (project / ".git").mkdir()
            scaffolder.scaffold(
                config(
                    project,
                    github_repository=None,
                    authors=(),
                    omit_ai_disclosure=True,
                    platforms=("linux-aarch64",),
                )
            )

            readme = (project / "README.md").read_text(encoding="utf-8")
            self.assertNotIn("AI-generated repository", readme)
            self.assertNotIn("github.io", readme)
            self.assertNotIn("pixi add --git", readme)

            docs_workflow = (project / ".github/workflows/docs.yml").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("  deploy:", docs_workflow)
            hugo = (project / "docs/site/hugo.yaml").read_text(encoding="utf-8")
            self.assertIn("baseURL: http://localhost:1313/", hugo)
            self.assertNotIn("name: GitHub", hugo)
            modo = (project / "modo.yaml").read_text(encoding="utf-8")
            self.assertNotIn("source-url:", modo)

            manifest = tomllib.loads(
                (project / "pixi.toml").read_text(encoding="utf-8")
            )
            self.assertNotIn("authors", manifest["workspace"])
            self.assertEqual(manifest["workspace"]["platforms"], ["linux-aarch64"])

    def test_rejects_invalid_or_nonempty_targets_without_mutating_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            marker = project / "notes.txt"
            marker.write_text("user data", encoding="utf-8")

            with self.assertRaises(scaffolder.ScaffoldError):
                scaffolder.scaffold(config(project))
            self.assertEqual(marker.read_text(encoding="utf-8"), "user data")
            self.assertEqual(
                sorted(path.name for path in project.iterdir()), ["notes.txt"]
            )

            invalid = config(
                root / "invalid",
                package_name="Not Valid",
                github_repository="not-a-slug",
            )
            with self.assertRaises(scaffolder.ScaffoldError):
                scaffolder.render_files(invalid)
            self.assertFalse((root / "invalid").exists())

            with self.assertRaises(scaffolder.ScaffoldError):
                scaffolder.render_files(
                    config(root / "short-tool-version", mojo_version="1.0")
                )
            self.assertFalse((root / "short-tool-version").exists())

            with self.assertRaises(scaffolder.ScaffoldError):
                scaffolder.render_files(
                    config(root / "invalid-rust-name", rust_crate="bad.crate")
                )

            for overrides in (
                {"default_branch": "feature//unsafe"},
                {"skill_repository": "https://user@example.com/skill"},
                {"skill_repository": "https://example.com/@PACKAGE_NAME@"},
            ):
                with (
                    self.subTest(overrides=overrides),
                    self.assertRaises(scaffolder.ScaffoldError),
                ):
                    scaffolder.render_files(config(root / "invalid-url", **overrides))

    def test_cli_reports_created_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "cli-project"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--project",
                    str(project),
                    "--package-name",
                    "cli-bind",
                    "--mojo-package",
                    "cli_bind",
                    "--rust-crate",
                    "cli-crate",
                    "--rust-version",
                    "1.2.3",
                    "--mojo-version",
                    "1.0.0",
                    "--platform",
                    "osx-arm64",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            report = json.loads(result.stdout)
            self.assertEqual(Path(report["project"]), project.resolve())
            self.assertIn("pixi.toml", report["created"])
            self.assertEqual(
                tomllib.loads((project / "pixi.toml").read_text(encoding="utf-8"))[
                    "workspace"
                ]["platforms"],
                ["osx-arm64"],
            )


if __name__ == "__main__":
    unittest.main()
