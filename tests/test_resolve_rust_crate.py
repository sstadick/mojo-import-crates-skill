from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "resolve_rust_crate.py"
SPEC = importlib.util.spec_from_file_location("resolve_rust_crate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


class RegistrySpecTests(unittest.TestCase):
    def test_exact_spec(self) -> None:
        self.assertEqual(
            resolver.parse_registry_spec("sample-crate@1.3.0"),
            ("sample-crate", "1.3.0"),
        )

    def test_range_is_rejected(self) -> None:
        for spec in ("sample-crate@^1.3", "sample-crate@1.*", "sample-crate@>=1"):
            with self.subTest(spec=spec), self.assertRaises(resolver.ResolveError):
                resolver.parse_registry_spec(spec)


class GitRevisionTests(unittest.TestCase):
    def test_only_full_commit_hash_is_accepted_by_resolution_contract(self) -> None:
        for revision in ("main", "v1.2.3", "deadbeef", "g" * 40):
            with self.subTest(revision=revision), self.assertRaises(
                resolver.ResolveError
            ):
                resolver.validate_git_revision(revision)

    def test_full_commit_hash_is_normalized(self) -> None:
        revision = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
        self.assertEqual(resolver.validate_git_revision(revision), revision.lower())

    def test_remote_git_urls_are_accepted_without_credentials(self) -> None:
        for url in (
            "https://github.com/example/project.git",
            "ssh://git@github.com/example/project.git",
            "git://github.com/example/project.git",
            "git@github.com:example/project.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(resolver.validate_git_url(url), url)

    def test_local_credentialed_and_ambiguous_git_urls_are_rejected(self) -> None:
        for url in (
            "file:///tmp/project",
            "../project",
            "https://token@github.com/example/project.git",
            "https://user:token@github.com/example/project.git",
            "https://github.com/example/project.git?token=secret",
            "https://github.com/example/project.git#main",
        ):
            with self.subTest(url=url), self.assertRaises(resolver.ResolveError):
                resolver.validate_git_url(url)


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_ignores_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "data").mkdir()
            (root / "src" / "target").mkdir()
            (root / "target").mkdir()
            (root / "Cargo.toml").write_text(
                '[package]\nname = "local"\nversion = "0.1.0"\n', encoding="utf-8"
            )
            (root / "src" / "lib.rs").write_text("pub fn value() {}\n", encoding="utf-8")
            (root / "data" / "schema.bin").write_bytes(b"first")
            first = resolver.local_fingerprint(root)
            (root / "target" / "noise").write_text("ignored\n", encoding="utf-8")
            self.assertEqual(first, resolver.local_fingerprint(root))
            (root / "src" / "target" / "module.rs").write_text(
                "pub fn nested_target_module() {}\n", encoding="utf-8"
            )
            self.assertNotEqual(first, resolver.local_fingerprint(root))
            first = resolver.local_fingerprint(root)
            (root / "src" / "lib.rs").write_text("pub fn changed() {}\n", encoding="utf-8")
            self.assertNotEqual(first, resolver.local_fingerprint(root))

            second = resolver.local_fingerprint(root)
            (root / "data" / "schema.bin").write_bytes(b"second")
            self.assertNotEqual(second, resolver.local_fingerprint(root))

    def test_fingerprint_rejects_file_and_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory)
            root = outer / "crate"
            external = outer / "external"
            (root / "src").mkdir(parents=True)
            external.mkdir()
            (root / "Cargo.toml").write_text(
                '[package]\nname = "local"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            (root / "src" / "lib.rs").write_text("pub fn value() {}\n", encoding="utf-8")
            (external / "data.txt").write_text("outside\n", encoding="utf-8")

            file_link = root / "linked-data.txt"
            file_link.symlink_to(external / "data.txt")
            with self.assertRaisesRegex(resolver.ResolveError, "do not follow symlinks"):
                resolver.local_fingerprint(root)
            file_link.unlink()

            directory_link = root / "linked-directory"
            directory_link.symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(resolver.ResolveError, "do not follow symlinks"):
                resolver.local_fingerprint(root)


class ManifestTests(unittest.TestCase):
    def test_registry_dependency_is_exact_and_features_sorted(self) -> None:
        table = resolver.dependency_table(
            package_name="sample-crate",
            version="1.3.0",
            features=["z", "a", "a"],
        )
        self.assertIn('version = "=1.3.0"', table)
        self.assertIn('features = ["a", "z"]', table)

    def test_default_features_can_be_disabled(self) -> None:
        table = resolver.dependency_table(
            package_name="generic-crate",
            version="1.2.3",
            features=[],
            default_features=False,
        )
        self.assertIn("default-features = false", table)


@unittest.skipUnless(shutil.which("cargo"), "Cargo is required for local resolution")
class LocalWorkspaceTests(unittest.TestCase):
    def test_workspace_member_path_and_default_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            member = root / "crates" / "selected"
            (member / "src").mkdir(parents=True)
            (root / "Cargo.toml").write_text(
                '[workspace]\nmembers = ["crates/selected"]\nresolver = "2"\n',
                encoding="utf-8",
            )
            (member / "Cargo.toml").write_text(
                """\
[package]
name = "selected-member"
version = "0.2.0"
edition = "2021"

[features]
default = ["enabled"]
enabled = []
""",
                encoding="utf-8",
            )
            (member / "src" / "lib.rs").write_text(
                "pub fn selected() {}\n",
                encoding="utf-8",
            )

            args = resolver.build_parser().parse_args(
                [
                    "--path",
                    str(root),
                    "--package",
                    "selected-member",
                    "--no-default-features",
                    "--offline",
                ]
            )
            result = resolver.resolve(args)
            self.assertEqual(result["request"]["path"], str(root.resolve()))
            self.assertEqual(result["request"]["package_path"], str(member.resolve()))
            self.assertFalse(result["request"]["default_features"])
            self.assertEqual(result["resolved"]["enabled_features"], [])


if __name__ == "__main__":
    unittest.main()
