from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "integrate_binding.py"
SPEC = importlib.util.spec_from_file_location("integrate_binding", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
integrator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(integrator)


HAT_MANIFEST = textwrap.dedent(
    """\
    [workspace]
    channels = ["https://prefix.dev/conda-forge"]
    platforms = ["linux-64", "linux-aarch64", "osx-arm64"]
    preview = ["pixi-build"]

    [package]
    name = "sample_app"
    version = "0.4.0"

    [package.build]
    backend = { name = "pixi-build-mojo", version = "0.*" }

    [package.build-dependencies]
    mojo-compiler = "=1.0.0"

    [package.run-dependencies]
    mojo-compiler = "=1.0.0"

    [dependencies]
    mojo = "=1.0.0"
    """
)

REGISTRY_SOURCE = "registry+https://example.invalid/index"
GIT_URL = "https://example.invalid/upstream.git"
GIT_REV = "0123456789abcdef0123456789abcdef01234567"
EXPECTED_TOOLS = {
    "generator_version": "0.1.0",
    "abi_backend_version": "0.1.0",
    "diplomat_version": "0.16.1",
    "diplomat_core_version": "0.16.1",
    "diplomat_runtime_version": "0.16.0",
    "mojo_version": "1.0.0",
}


def fixture_checksum(crate_name: str) -> str:
    return hashlib.sha256(crate_name.encode("utf-8")).hexdigest()


def write_project(root: Path, manifest: str = HAT_MANIFEST) -> Path:
    project = root / "project"
    project.mkdir()
    (project / "pixi.toml").write_text(manifest, encoding="utf-8")
    return project


def write_binding(
    root: Path,
    *,
    binding_id: str,
    crate_name: str,
    ffi_crate: str,
    mojo_package: str,
    specialization: str,
    features: tuple[str, ...] = (),
    default_features: bool = True,
    source_kind: str = "registry",
) -> Path:
    binding = root / f"staging-{binding_id}"
    (binding / "ffi" / "src").mkdir(parents=True)
    (binding / "ffi" / "tests").mkdir()
    (binding / "mojo" / mojo_package).mkdir(parents=True)
    (binding / "tests").mkdir()
    checksum = fixture_checksum(crate_name)
    symbol_prefix = "rust_mojo__" + binding_id.replace("-", "_") + "__"
    rust_module = crate_name.replace("-", "_")
    source_fields = (
        f'checksum = "{checksum}"'
        if source_kind == "registry"
        else f'git = "{GIT_URL}"\nrev = "{GIT_REV}"'
    )
    feature_toml = json.dumps(list(features))
    (binding / "binding.toml").write_text(
        textwrap.dedent(
            f'''\
            schema_version = 1

            [binding]
            id = "{binding_id}"
            mojo_package = "{mojo_package}"
            symbol_prefix = "{symbol_prefix}"

            [crate]
            name = "{crate_name}"
            version = "=2.3.4"
            source_kind = "{source_kind}"
            features = {feature_toml}
            default_features = {str(default_features).lower()}
            {source_fields}

            [tools]
            generator_version = "0.1.0"
            abi_backend_version = "0.1.0"
            diplomat_version = "0.16.1"
            diplomat_core_version = "0.16.1"
            diplomat_runtime_version = "0.16.0"
            mojo_version = "1.0.0"

            [ffi]
            crate_name = "{ffi_crate}"
            cargo_manifest = "ffi/Cargo.toml"
            tests = ["ffi/tests/bridge.rs"]

            [mojo]
            source_dir = "mojo/{mojo_package}"
            tests = ["tests/smoke.mojo"]
            types = []

            [scope]
            requested = ["{rust_module}::parse"]
            trait_policy = "No trait APIs are in this integration fixture's scope."
            audited_exports = ["parse"]

            [[specializations]]
            rust_type = "{specialization}"
            mojo_name = "FixtureSpecialization"
            parameters = {{ element_type = "u16", inline_capacity = "12" }}
            chosen_by = "user"
            exports = ["parse"]

            [[exports]]
            id = "parse"
            rust = "{rust_module}::parse"
            mojo = "parse"
            status = "MONOMORPHIZED"
            reason = "The fixture records one explicit user-approved specialization."

            [[mojo.functions]]
            rust_name = "parse"
            abi_symbol = "{symbol_prefix}parse"
            mojo_name = "parse"
            kind = "free"
            export = "parse"
            '''
        ),
        encoding="utf-8",
    )
    dependency_source = (
        'version = "=2.3.4"'
        if source_kind == "registry"
        else f'git = "{GIT_URL}", rev = "{GIT_REV}"'
    )
    upstream_alias = crate_name
    upstream_package = ""
    if crate_name in {"diplomat", "diplomat-runtime"}:
        upstream_alias = f"upstream-{crate_name}"
        upstream_package = f'package = "{crate_name}", '
    (binding / "ffi" / "Cargo.toml").write_text(
        textwrap.dedent(
            f'''\
            [package]
            name = "{ffi_crate}"
            version = "0.1.0"
            edition = "2024"

            [lib]
            name = "{ffi_crate}"
            crate-type = ["cdylib", "rlib"]

            [dependencies]
            "{upstream_alias}" = {{ {upstream_package}{dependency_source}, default-features = {str(default_features).lower()}, features = {feature_toml} }}
            diplomat = "=0.16.1"
            diplomat-runtime = "=0.16.0"
            '''
        ),
        encoding="utf-8",
    )
    locked_source = (
        REGISTRY_SOURCE
        if source_kind == "registry"
        else f"git+{GIT_URL}?rev={GIT_REV}#{GIT_REV}"
    )
    checksum_line = (
        f'checksum = "{checksum}"\n' if source_kind == "registry" else ""
    )
    root_identities = [
        (crate_name, "2.3.4"),
        ("diplomat", "0.16.1"),
        ("diplomat-runtime", "0.16.0"),
    ]
    locked_identities = root_identities + [("diplomat_core", "0.16.1")]
    root_name_counts = {
        name: sum(candidate == name for candidate, _ in locked_identities)
        for name, _ in locked_identities
    }
    root_dependencies = [
        name if root_name_counts[name] == 1 else f"{name} {version}"
        for name, version in root_identities
    ]
    core_dependency = (
        "diplomat_core 0.16.1"
        if crate_name == "diplomat_core"
        else "diplomat_core"
    )
    (binding / "ffi" / "Cargo.lock").write_text(
        textwrap.dedent(
            f'''\
            version = 4

            [[package]]
            name = "{ffi_crate}"
            version = "0.1.0"
            dependencies = {json.dumps(root_dependencies)}

            [[package]]
            name = "diplomat"
            version = "0.16.1"
            source = "{REGISTRY_SOURCE}"
            dependencies = ["{core_dependency}"]

            [[package]]
            name = "diplomat_core"
            version = "0.16.1"
            source = "{REGISTRY_SOURCE}"

            [[package]]
            name = "diplomat-runtime"
            version = "0.16.0"
            source = "{REGISTRY_SOURCE}"

            [[package]]
            name = "{crate_name}"
            version = "2.3.4"
            source = "{locked_source}"
            {checksum_line}'''
        ),
        encoding="utf-8",
    )
    (binding / "ffi" / "src" / "lib.rs").write_text(
        textwrap.dedent(
            f'''\
            #[diplomat::bridge]
            #[diplomat::abi_rename = "{symbol_prefix}{{0}}"]
            mod ffi {{
                pub fn parse() -> usize {{ 0 }}
            }}
            '''
        ),
        encoding="utf-8",
    )
    (binding / "ffi" / "tests" / "bridge.rs").write_text(
        "#[test]\nfn bridge_smoke() { assert_eq!(2 + 2, 4); }\n",
        encoding="utf-8",
    )
    body = textwrap.dedent(
        f'''\
        comptime parse_abi = def() thin abi("C") -> UInt
        # symbol parse_abi = "{symbol_prefix}parse"
        '''
    )
    report: dict[str, object] = {
        "schema_version": 3,
        "backend": {
            "name": "diplomat-gen-mojo",
            "version": "0.1.0",
            "diplomat_core_version": "0.16.1",
        },
        "mojo_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "structs": [],
        "opaques": [],
        "enums": [],
        "functions": [
            {
                "owner": None,
                "rust_name": "parse",
                "abi_name": symbol_prefix + "parse",
                "mojo_abi_alias": "parse_abi",
                "params": [],
                "output": {"kind": "primitive", "details": "u_int"},
            }
        ],
        "types": [],
        "unsupported": [],
    }
    model_payload = {
        key: report[key]
        for key in ("structs", "opaques", "enums", "functions", "types", "unsupported")
    }
    model_hash = hashlib.sha256(
        json.dumps(
            model_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    report["abi_model_sha256"] = model_hash
    (binding / "abi-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (binding / "mojo" / mojo_package / "_ffi.mojo").write_text(
        textwrap.dedent(
            f'''\
            # GENERATED FILE — DO NOT EDIT DIRECTLY
            # Generator: diplomat-gen-mojo 0.1.0
            # Diplomat core: 0.16.1
            # ABI model SHA-256: {model_hash}

            '''
        )
        + body,
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(integrator._WRAPPER_GENERATOR),
            "--binding",
            str(binding / "binding.toml"),
            "--report",
            str(binding / "abi-report.json"),
            "--output-dir",
            str(binding / "mojo" / mojo_package),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    (binding / "tests" / "smoke.mojo").write_text(
        "def main() raises:\n    pass\n", encoding="utf-8"
    )
    return binding


def invoke(*arguments: str) -> tuple[int, dict[str, object] | None, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = integrator.main(list(arguments))
    parsed = json.loads(stdout.getvalue()) if stdout.getvalue() else None
    return result, parsed, stderr.getvalue()


def resolution_for(binding: Path, *, ephemeral: bool = False) -> str:
    manifest = tomllib.loads((binding / "binding.toml").read_text(encoding="utf-8"))
    crate = manifest["crate"]
    kind = crate["source_kind"]
    request: dict[str, object] = {
        "kind": kind,
        "name": crate["name"],
        "features": crate["features"],
        "default_features": crate["default_features"],
    }
    if kind == "registry":
        request["version"] = crate["version"]
        source = REGISTRY_SOURCE
        checksum: str | None = crate["checksum"]
    else:
        request["git"] = crate["git"]
        request["rev"] = crate["rev"]
        source = f"git+{crate['git']}?rev={crate['rev']}#{crate['rev']}"
        checksum = None
    resolved: dict[str, object] = {
        "name": crate["name"],
        "version": crate["version"].removeprefix("="),
        "source": source,
        "checksum": checksum,
    }
    if kind == "git":
        resolved["git_commit"] = crate["rev"]
    if ephemeral:
        resolved["manifest_path"] = "/tmp/ephemeral/source/Cargo.toml"
    payload: dict[str, object] = {
        "schema_version": 1,
        "request": request,
        "resolved": resolved,
    }
    if ephemeral:
        payload["cargo_version"] = "cargo 99.0.0 (/tmp/build)"
    return json.dumps(payload)


def install_binding(
    project: Path, binding: Path
) -> tuple[int, dict[str, object] | None, str]:
    return invoke(
        "--project",
        str(project),
        "--binding-root",
        str(binding),
        "--resolution",
        resolution_for(binding),
    )


class GeneralIntegrationTests(unittest.TestCase):
    def test_closed_generated_mojo_contract_is_required_before_mutation(self) -> None:
        cases = {
            "missing ABI report": (
                lambda binding: (binding / "abi-report.json").unlink(),
                "requires a real ABI report",
            ),
            "stale wrapper": (
                lambda binding: (binding / "mojo/contract/_wrappers.mojo").write_text(
                    "# stale wrapper\n", encoding="utf-8"
                ),
                "Generated Mojo package is stale: _wrappers.mojo",
            ),
            "raw ABI drift": (
                lambda binding: (binding / "mojo/contract/_ffi.mojo").write_text(
                    (binding / "mojo/contract/_ffi.mojo").read_text(encoding="utf-8")
                    + "# unreported symbol\n",
                    encoding="utf-8",
                ),
                "_ffi.mojo ABI body does not match abi-report.json",
            ),
        }
        for label, (mutate, expected) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = write_project(root)
                binding = write_binding(
                    root,
                    binding_id="contract",
                    crate_name="semver",
                    ffi_crate="contract_ffi",
                    mojo_package="contract",
                    specialization="semver::Version",
                )
                mutate(binding)
                before = {
                    path.relative_to(project).as_posix(): path.read_bytes()
                    for path in project.rglob("*")
                    if path.is_file()
                }

                refused, report, error = install_binding(project, binding)

                self.assertEqual(refused, 2)
                self.assertIsNone(report)
                self.assertIn(expected, error)
                self.assertEqual(
                    before,
                    {
                        path.relative_to(project).as_posix(): path.read_bytes()
                        for path in project.rglob("*")
                        if path.is_file()
                    },
                )

    def test_tool_provenance_matches_cargo_lock_and_normalized_mojo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mixed_exact_syntax = HAT_MANIFEST.replace(
                'mojo-compiler = "=1.0.0"',
                'mojo-compiler = "==1.0.0"',
                1,
            )
            project = write_project(root, mixed_exact_syntax)
            binding = write_binding(
                root,
                binding_id="provenance",
                crate_name="semver",
                ffi_crate="provenance_ffi",
                mojo_package="provenance",
                specialization="semver::Version",
            )
            result, report, error = install_binding(project, binding)
            self.assertEqual((result, error), (0, ""))
            self.assertIsNotNone(report)
            provenance = json.loads(
                (
                    project
                    / "vendor/rust-bindings/provenance/manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["tools"], EXPECTED_TOOLS)
            recipe = (
                project / "vendor/rust-bindings/recipe.yaml"
            ).read_text(encoding="utf-8")
            self.assertIn('"mojo-compiler ==1.0.0"', recipe)

    def test_tools_table_is_required_closed_and_exact(self) -> None:
        cases = {
            "missing table": (
                lambda text: text.replace("[tools]", "[tool_versions]", 1),
                "requires a [tools] table",
            ),
            "missing key": (
                lambda text: text.replace('abi_backend_version = "0.1.0"\n', "", 1),
                "missing keys: abi_backend_version",
            ),
            "unknown key": (
                lambda text: text.replace(
                    'mojo_version = "1.0.0"',
                    'mojo_version = "1.0.0"\nunreviewed_tool = "1.0.0"',
                    1,
                ),
                "unknown keys: unreviewed_tool",
            ),
            "non-exact version": (
                lambda text: text.replace(
                    'generator_version = "0.1.0"',
                    'generator_version = "main"',
                    1,
                ),
                "exact semver-like version",
            ),
        }
        for label, (mutation, expected) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = write_project(root)
                binding = write_binding(
                    root,
                    binding_id="tool_schema",
                    crate_name="semver",
                    ffi_crate="tool_schema_ffi",
                    mojo_package="tool_schema",
                    specialization="semver::Version",
                )
                manifest = binding / "binding.toml"
                manifest.write_text(
                    mutation(manifest.read_text(encoding="utf-8")), encoding="utf-8"
                )
                refused, _, error = install_binding(project, binding)
                self.assertEqual(refused, 2)
                self.assertIn(expected, error)
                self.assertFalse((project / "vendor").exists())

    def test_diplomat_dependencies_must_match_tools_and_be_nonoptional(self) -> None:
        cases = {
            "version drift": (
                lambda text: text.replace(
                    'diplomat = "=0.16.1"', 'diplomat = "=0.16.2"', 1
                ),
                "match [tools].diplomat_version",
            ),
            "missing direct dependency": (
                lambda text: text.replace('diplomat-runtime = "=0.16.0"\n', "", 1),
                "directly depend exactly once on 'diplomat-runtime'",
            ),
            "optional runtime": (
                lambda text: text.replace(
                    'diplomat-runtime = "=0.16.0"',
                    'diplomat-runtime = { version = "=0.16.0", optional = true }',
                    1,
                ),
                "must be non-optional",
            ),
        }
        for label, (mutation, expected) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = write_project(root)
                binding = write_binding(
                    root,
                    binding_id="cargo_tools",
                    crate_name="semver",
                    ffi_crate="cargo_tools_ffi",
                    mojo_package="cargo_tools",
                    specialization="semver::Version",
                )
                cargo = binding / "ffi/Cargo.toml"
                cargo.write_text(
                    mutation(cargo.read_text(encoding="utf-8")), encoding="utf-8"
                )
                refused, _, error = install_binding(project, binding)
                self.assertEqual(refused, 2)
                self.assertIn(expected, error)
                self.assertFalse((project / "vendor").exists())

    def test_cargo_lock_must_bind_root_and_selected_tool_versions(self) -> None:
        cases = {
            "root dependency missing": (
                lambda text: text.replace(', "diplomat"', "", 1),
                "does not lock exactly one direct 'diplomat' dependency",
            ),
            "root dependency version drift": (
                lambda text: text.replace(
                    '"diplomat", "diplomat-runtime"',
                    '"diplomat 0.16.2", "diplomat-runtime"',
                    1,
                ),
                "matching [tools].diplomat_version",
            ),
            "diplomat version drift": (
                lambda text: text.replace(
                    'name = "diplomat"\nversion = "0.16.1"',
                    'name = "diplomat"\nversion = "0.16.2"',
                    1,
                ),
                "locked diplomat version does not match",
            ),
            "core version drift": (
                lambda text: text.replace(
                    'name = "diplomat_core"\nversion = "0.16.1"',
                    'name = "diplomat_core"\nversion = "0.16.2"',
                    1,
                ),
                "locked diplomat_core version does not match",
            ),
            "duplicate runtime": (
                lambda text: text
                + '\n[[package]]\nname = "diplomat-runtime"\nversion = "0.16.0"\n'
                + f'source = "{REGISTRY_SOURCE}"\n',
                "exactly one locked 'diplomat-runtime' package",
            ),
        }
        for label, (mutation, expected) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = write_project(root)
                binding = write_binding(
                    root,
                    binding_id="lock_tools",
                    crate_name="semver",
                    ffi_crate="lock_tools_ffi",
                    mojo_package="lock_tools",
                    specialization="semver::Version",
                )
                lock = binding / "ffi/Cargo.lock"
                lock.write_text(
                    mutation(lock.read_text(encoding="utf-8")), encoding="utf-8"
                )
                refused, _, error = install_binding(project, binding)
                self.assertEqual(refused, 2)
                self.assertIn(expected, error)
                self.assertFalse((project / "vendor").exists())

    def test_unrelated_locked_diplomat_versions_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="multi_version_tools",
                crate_name="semver",
                ffi_crate="multi_version_tools_ffi",
                mojo_package="multi_version_tools",
                specialization="semver::Version",
            )
            lock = binding / "ffi/Cargo.lock"
            lock_text = lock.read_text(encoding="utf-8")
            lock_text = lock_text.replace(
                'dependencies = ["semver", "diplomat", "diplomat-runtime"]',
                'dependencies = ["semver", "diplomat 0.16.1", '
                '"diplomat-runtime 0.16.0"]',
                1,
            ).replace(
                'dependencies = ["diplomat_core"]',
                'dependencies = ["diplomat_core 0.16.1"]',
                1,
            )
            checksum_marker = f'checksum = "{fixture_checksum("semver")}"'
            lock_text = lock_text.replace(
                checksum_marker,
                'dependencies = ["diplomat 9.1.0", '
                '"diplomat_core 9.2.0", "diplomat-runtime 9.3.0"]\n'
                + checksum_marker,
                1,
            )
            lock_text += textwrap.dedent(
                f'''\

                [[package]]
                name = "diplomat"
                version = "9.1.0"
                source = "{REGISTRY_SOURCE}"

                [[package]]
                name = "diplomat_core"
                version = "9.2.0"
                source = "{REGISTRY_SOURCE}"

                [[package]]
                name = "diplomat-runtime"
                version = "9.3.0"
                source = "{REGISTRY_SOURCE}"
                '''
            )
            lock.write_text(lock_text, encoding="utf-8")

            result, report, error = install_binding(project, binding)
            self.assertEqual((result, error), (0, ""))
            self.assertIsNotNone(report)
            provenance = json.loads(
                (
                    project
                    / "vendor/rust-bindings/multi_version_tools/manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["tools"], EXPECTED_TOOLS)

    def test_upstream_diplomat_package_names_do_not_collide_with_tool_roles(self) -> None:
        for crate_name in ("diplomat", "diplomat-runtime", "diplomat_core"):
            with self.subTest(crate_name=crate_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = write_project(root)
                binding_id = "upstream_" + crate_name.replace("-", "_")
                binding = write_binding(
                    root,
                    binding_id=binding_id,
                    crate_name=crate_name,
                    ffi_crate=binding_id + "_ffi",
                    mojo_package=binding_id,
                    specialization=crate_name.replace("-", "_") + "::PublicType",
                )

                result, report, error = install_binding(project, binding)
                self.assertEqual((result, error), (0, ""))
                self.assertIsNotNone(report)
                self.assertTrue(
                    (
                        project
                        / "vendor/rust-bindings"
                        / binding_id
                        / "manifest.json"
                    ).is_file()
                )

    def test_selected_diplomat_core_edge_is_required_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="wrong_core_edge",
                crate_name="semver",
                ffi_crate="wrong_core_edge_ffi",
                mojo_package="wrong_core_edge",
                specialization="semver::Version",
            )
            lock = binding / "ffi/Cargo.lock"
            lock.write_text(
                lock.read_text(encoding="utf-8").replace(
                    'dependencies = ["diplomat_core"]',
                    'dependencies = ["diplomat_core 9.9.9"]',
                    1,
                )
                + textwrap.dedent(
                    f'''\

                    [[package]]
                    name = "diplomat_core"
                    version = "9.9.9"
                    source = "{REGISTRY_SOURCE}"
                    '''
                ),
                encoding="utf-8",
            )

            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn(
                "selected diplomat package does not lock exactly one "
                "diplomat_core dependency",
                error,
            )
            self.assertFalse((project / "vendor").exists())

    def test_mojo_tool_version_mismatch_fails_before_project_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root, HAT_MANIFEST.replace("1.0.0", "1.1.0"))
            binding = write_binding(
                root,
                binding_id="mojo_mismatch",
                crate_name="semver",
                ffi_crate="mojo_mismatch_ffi",
                mojo_package="mojo_mismatch",
                specialization="semver::Version",
            )
            before = {
                path.relative_to(project).as_posix(): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file()
            }
            refused, _, error = install_binding(project, binding)
            after = {
                path.relative_to(project).as_posix(): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file()
            }
            self.assertEqual(refused, 2)
            self.assertIn("does not match binding [tools].mojo_version", error)
            self.assertEqual(after, before)

    def test_build_mode_omits_publish_metadata_for_legacy_pixi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="legacy_parser",
                crate_name="nom",
                ffi_crate="legacy_parser_ffi",
                mojo_package="legacy_parser",
                specialization="nom::Parser<&[u8]>",
            )
            result, report, error = install_binding(project, binding)
            self.assertEqual((result, error), (0, ""))
            assert report is not None
            self.assertEqual(report["artifact_mode"], "build")
            self.assertEqual(report["artifact_mode_source"], "compatible-default")
            self.assertEqual(
                report["artifact_commands"],
                [
                    "pixi build --manifest-path vendor/rust-bindings/pixi.toml "
                    "--output-dir <directory>",
                    "pixi build --output-dir <directory>",
                ],
            )

            root_manifest = tomllib.loads(
                (project / "pixi.toml").read_text(encoding="utf-8")
            )
            nested_manifest = tomllib.loads(
                (project / "vendor/rust-bindings/pixi.toml").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("publish", root_manifest["package"])
            self.assertNotIn("publish", nested_manifest["package"])
            self.assertEqual(
                invoke(
                    "--project",
                    str(project),
                    "--binding-root",
                    str(binding),
                    "--check",
                )[0],
                0,
            )

    def test_requires_pixi_selects_publish_and_explicit_build_overrides_it(self) -> None:
        modern_manifest = HAT_MANIFEST.replace(
            'preview = ["pixi-build"]',
            'preview = ["pixi-build"]\nrequires-pixi = ">=0.76.0, <1"',
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root, modern_manifest)
            binding = write_binding(
                root,
                binding_id="modern_parser",
                crate_name="nom",
                ffi_crate="modern_parser_ffi",
                mojo_package="modern_parser",
                specialization="nom::Parser<&[u8]>",
            )
            result, report, error = install_binding(project, binding)
            self.assertEqual((result, error), (0, ""))
            assert report is not None
            self.assertEqual(report["artifact_mode"], "publish")
            self.assertEqual(report["artifact_mode_source"], "requires-pixi")
            self.assertEqual(
                report["artifact_commands"],
                ["pixi publish --target-dir <directory>"],
            )
            for manifest_path in (
                project / "pixi.toml",
                project / "vendor/rust-bindings/pixi.toml",
            ):
                manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertIs(manifest["package"]["publish"], True)

            check_result, check_report, check_error = invoke(
                "--project",
                str(project),
                "--binding-root",
                str(binding),
                "--artifact-mode",
                "build",
                "--check",
            )
            self.assertEqual((check_result, check_error), (1, ""))
            assert check_report is not None
            self.assertEqual(check_report["artifact_mode_source"], "explicit")
            self.assertIn("pixi.toml", check_report["changed"])
            self.assertIn(
                "vendor/rust-bindings/pixi.toml", check_report["changed"]
            )

            changed_result, changed_report, changed_error = invoke(
                "--project",
                str(project),
                "--binding-root",
                str(binding),
                "--artifact-mode",
                "build",
            )
            self.assertEqual((changed_result, changed_error), (0, ""))
            assert changed_report is not None
            self.assertEqual(changed_report["artifact_mode"], "build")
            for manifest_path in (
                project / "pixi.toml",
                project / "vendor/rust-bindings/pixi.toml",
            ):
                manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertNotIn("publish", manifest["package"])
            self.assertEqual(
                invoke(
                    "--project",
                    str(project),
                    "--binding-root",
                    str(binding),
                    "--artifact-mode",
                    "build",
                    "--check",
                )[0],
                0,
            )

    def test_user_publish_setting_is_preserved_and_never_claimed(self) -> None:
        user_manifest = HAT_MANIFEST.replace(
            '[package]\nname = "sample_app"',
            '[package]\nname = "sample_app"\npublish = true',
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root, user_manifest)
            binding = write_binding(
                root,
                binding_id="user_config",
                crate_name="nom",
                ffi_crate="user_config_ffi",
                mojo_package="user_config",
                specialization="nom::Parser<&[u8]>",
            )
            result, report, error = invoke(
                "--project",
                str(project),
                "--binding-root",
                str(binding),
                "--resolution",
                resolution_for(binding),
                "--artifact-mode",
                "build",
            )
            self.assertEqual((result, error), (0, ""))
            assert report is not None
            self.assertEqual(len(report["warnings"]), 1)
            self.assertIn("user-owned", report["warnings"][0])
            root_document = tomllib.loads(
                (project / "pixi.toml").read_text(encoding="utf-8")
            )
            nested_document = tomllib.loads(
                (project / "vendor/rust-bindings/pixi.toml").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIs(root_document["package"]["publish"], True)
            self.assertNotIn("publish", nested_document["package"])
            aggregate = json.loads(
                (project / "vendor/rust-bindings/manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIs(aggregate["root_manifest"]["publish_owned"], False)
            self.assertEqual(
                invoke(
                    "--project",
                    str(project),
                    "--binding-root",
                    str(binding),
                    "--artifact-mode",
                    "build",
                    "--check",
                )[0],
                0,
            )

            publish_result, publish_report, publish_error = invoke(
                "--project",
                str(project),
                "--binding-root",
                str(binding),
                "--artifact-mode",
                "publish",
            )
            self.assertEqual((publish_result, publish_error), (0, ""))
            assert publish_report is not None
            self.assertEqual(
                publish_report["artifact_commands"],
                ["pixi publish --target-dir <directory>"],
            )
            root_document = tomllib.loads(
                (project / "pixi.toml").read_text(encoding="utf-8")
            )
            nested_document = tomllib.loads(
                (project / "vendor/rust-bindings/pixi.toml").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIs(root_document["package"]["publish"], True)
            self.assertIs(nested_document["package"]["publish"], True)
            aggregate = json.loads(
                (project / "vendor/rust-bindings/manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIs(aggregate["root_manifest"]["publish_owned"], False)

    def test_publish_mode_rejects_user_disabled_root_without_mutation(self) -> None:
        disabled_manifest = HAT_MANIFEST.replace(
            '[package]\nname = "sample_app"',
            '[package]\nname = "sample_app"\npublish = false',
        )
        for selection in ("explicit", "inferred"):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = disabled_manifest
                if selection == "inferred":
                    manifest = manifest.replace(
                        'preview = ["pixi-build"]',
                        'preview = ["pixi-build"]\nrequires-pixi = ">=0.76"',
                    )
                project = write_project(root, manifest)
                binding = write_binding(
                    root,
                    binding_id=f"no_publish_{selection}",
                    crate_name="nom",
                    ffi_crate=f"no_publish_{selection}_ffi",
                    mojo_package=f"no_publish_{selection}",
                    specialization="nom::Parser<&[u8]>",
                )
                before = {
                    path.relative_to(project).as_posix(): path.read_bytes()
                    for path in project.rglob("*")
                    if path.is_file()
                }
                arguments = [
                    "--project",
                    str(project),
                    "--binding-root",
                    str(binding),
                    "--resolution",
                    resolution_for(binding),
                ]
                if selection == "explicit":
                    arguments.extend(("--artifact-mode", "publish"))

                result, report, error = invoke(*arguments)

                self.assertEqual(result, 2)
                self.assertIsNone(report)
                self.assertIn("[package].publish", error)
                self.assertIn("publish = false", error)
                self.assertIn("[package].publish = true", error)
                self.assertIn("--artifact-mode build", error)
                after = {
                    path.relative_to(project).as_posix(): path.read_bytes()
                    for path in project.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertFalse((project / "vendor").exists())

    def test_artifact_mode_inference_is_conservative(self) -> None:
        self.assertEqual(
            integrator.infer_artifact_mode(None),
            ("build", "compatible-default"),
        )
        self.assertEqual(
            integrator.infer_artifact_mode(">=0.75.9"),
            ("build", "compatible-default"),
        )
        self.assertEqual(
            integrator.infer_artifact_mode(">=0.76"),
            ("publish", "requires-pixi"),
        )
        self.assertEqual(
            integrator.infer_artifact_mode(">=0.76.0, <1"),
            ("publish", "requires-pixi"),
        )
        self.assertEqual(
            integrator.infer_artifact_mode(">=0.80 || <0.60"),
            ("build", "compatible-default"),
        )

    def test_two_unrelated_bindings_form_one_sorted_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            zeta = write_binding(
                root,
                binding_id="zeta_codec",
                crate_name="zstd-safe",
                ffi_crate="zeta_codec_ffi",
                mojo_package="zeta_codec",
                specialization="zstd_safe::Compressor<Vec<u8>>",
                features=("a", "z"),
            )
            alpha = write_binding(
                root,
                binding_id="alpha_time",
                crate_name="time",
                ffi_crate="alpha_time_ffi",
                mojo_package="alpha_time",
                specialization="time::Duration<i128>",
            )
            alpha_manifest = alpha / "binding.toml"
            alpha_manifest.write_text(
                alpha_manifest.read_text(encoding="utf-8")
                + textwrap.dedent(
                    """\

                    [pixi]
                    build_dependencies = ["pkg-config >=0.29", "cmake", "cmake"]
                    host_dependencies = ["openssl >=3"]
                    run_dependencies = ["zlib >=1.3"]
                    """
                ),
                encoding="utf-8",
            )
            first, _, first_error = invoke(
                "--project",
                str(project),
                "--binding-root",
                str(zeta),
                "--resolution",
                resolution_for(zeta, ephemeral=True),
            )
            second, report, second_error = install_binding(project, alpha)
            self.assertEqual((first, first_error), (0, ""))
            self.assertEqual((second, second_error), (0, ""))
            assert report is not None
            self.assertEqual(report["installed_bindings"], ["alpha_time", "zeta_codec"])

            recipe = (project / "vendor/rust-bindings/recipe.yaml").read_text(
                encoding="utf-8"
            )
            self.assertLess(
                recipe.index("alpha_time/ffi/Cargo.toml"),
                recipe.index("zeta_codec/ffi/Cargo.toml"),
            )
            for value in (
                "cargo test --release --locked",
                "--test bridge",
                "-- --list | grep -q ': test$'",
                "cargo build --release --locked",
                "--remap-path-prefix=${BUILD_PREFIX}=",
                "libalpha_time_ffi.so",
                "libzeta_codec_ffi.dylib",
                "mojo precompile alpha_time/mojo/alpha_time",
                'MODULAR_HOME=\\"$PREFIX/share/max\\" \\"$PREFIX/bin/mojo\\" run alpha_time/tests/smoke.mojo',
                'MODULAR_HOME=\\"$PREFIX/share/max\\" \\"$PREFIX/bin/mojo\\" run zeta_codec/tests/smoke.mojo',
                '"mojo-compiler ==1.0.0"',
                '"cmake"',
                '"pkg-config >=0.29"',
                '"openssl >=3"',
                '"zlib >=1.3"',
            ):
                self.assertIn(value, recipe)
            self.assertEqual(recipe.count('    - "cmake"'), 1)

            root_manifest = (project / "pixi.toml").read_text(encoding="utf-8")
            self.assertEqual(root_manifest.count('"sample-app-rust-mojo-bindings"'), 3)
            self.assertNotIn("alpha-time-rust-mojo-bindings", root_manifest)
            self.assertNotIn("zeta-codec-rust-mojo-bindings", root_manifest)
            copied_manifest = (
                project / "vendor/rust-bindings/alpha_time/binding.toml"
            ).read_text(encoding="utf-8")
            self.assertIn('element_type = "u16"', copied_manifest)
            self.assertIn('inline_capacity = "12"', copied_manifest)
            provenance = json.loads(
                (project / "vendor/rust-bindings/zeta_codec/manifest.json").read_text()
            )
            self.assertEqual(
                provenance["resolution"]["resolved"]["checksum"],
                fixture_checksum("zstd-safe"),
            )
            provenance_text = json.dumps(provenance, sort_keys=True)
            self.assertNotIn("manifest_path", provenance_text)
            self.assertNotIn("cargo_version", provenance_text)
            self.assertNotIn("/tmp/ephemeral", provenance_text)
            self.assertEqual(provenance["crate"]["features"], ["a", "z"])
            implementation = SCRIPT.read_text(encoding="utf-8")
            for assumption in (
                "rust-lapper",
                "rust_lapper",
                "Lapper",
                'get("I")',
                'get("T")',
            ):
                self.assertNotIn(assumption, implementation)

    def test_normalized_binding_namespace_collision_is_refused_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            dashed = write_binding(
                root,
                binding_id="foo-bar",
                crate_name="nom",
                ffi_crate="foo_dash_ffi",
                mojo_package="foo_dash_package",
                specialization="nom::Parser<&[u8]>",
            )
            underscored = write_binding(
                root,
                binding_id="foo_bar",
                crate_name="serde",
                ffi_crate="foo_under_ffi",
                mojo_package="foo_under_package",
                specialization="serde::Serializer<u8>",
            )
            self.assertEqual(install_binding(project, dashed)[0], 0)
            before_files = {
                path.relative_to(project).as_posix(): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file()
            }
            before_directories = {
                path.relative_to(project).as_posix()
                for path in project.rglob("*")
                if path.is_dir()
            }

            refused, report, error = install_binding(project, underscored)

            after_files = {
                path.relative_to(project).as_posix(): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file()
            }
            after_directories = {
                path.relative_to(project).as_posix()
                for path in project.rglob("*")
                if path.is_dir()
            }
            self.assertEqual(refused, 2)
            self.assertIsNone(report)
            self.assertIn("Normalized binding namespaces collide", error)
            self.assertIn("C symbol prefix 'rust_mojo__foo_bar__'", error)
            self.assertIn(
                "loader environment variable 'RUST_MOJO_FOO_BAR_LIBRARY'", error
            )
            self.assertIn("'foo-bar' and 'foo_bar'", error)
            self.assertEqual(after_files, before_files)
            self.assertEqual(after_directories, before_directories)
            self.assertFalse(
                (project / "vendor/rust-bindings/foo_bar").exists()
            )

    def test_rerun_check_hash_and_executable_modes_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="parser",
                crate_name="nom",
                ffi_crate="parser_ffi",
                mojo_package="parser",
                specialization="nom::Parser<&[u8]>",
            )
            helper = binding / "ffi/generate.sh"
            helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            helper.chmod(0o755)
            missing, _, error = invoke(
                "--project", str(project), "--binding-root", str(binding)
            )
            self.assertEqual(missing, 2)
            self.assertIn("First integration requires --resolution", error)
            self.assertEqual(install_binding(project, binding)[0], 0)
            checked, report, error = invoke(
                "--project", str(project), "--binding-root", str(binding), "--check"
            )
            self.assertEqual((checked, error), (0, ""))
            assert report is not None
            self.assertEqual(report["changed"], [])
            self.assertEqual(report["removed"], [])
            provenance = json.loads(
                (project / "vendor/rust-bindings/parser/manifest.json").read_text()
            )
            self.assertEqual(provenance["generated_modes"]["ffi/generate.sh"], 0o755)
            copied_helper = project / "vendor/rust-bindings/parser/ffi/generate.sh"
            self.assertTrue(copied_helper.stat().st_mode & 0o111)
            recipe_path = project / "vendor/rust-bindings/recipe.yaml"
            before = next(
                line
                for line in recipe_path.read_text().splitlines()
                if line.startswith("  string:")
            )
            helper.chmod(0o644)
            changed, report, _ = invoke(
                "--project", str(project), "--binding-root", str(binding), "--check"
            )
            self.assertEqual(changed, 1)
            assert report is not None
            self.assertIn("vendor/rust-bindings/parser/ffi/generate.sh", report["changed"])
            self.assertEqual(install_binding(project, binding)[0], 0)
            after = next(
                line
                for line in recipe_path.read_text().splitlines()
                if line.startswith("  string:")
            )
            self.assertNotEqual(before, after)
            self.assertEqual(
                invoke(
                    "--project", str(project), "--binding-root", str(binding), "--check"
                )[0],
                0,
            )

    def test_modified_generated_files_and_dependency_conflicts_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="numbers",
                crate_name="num-traits",
                ffi_crate="numbers_ffi",
                mojo_package="numbers",
                specialization="num_traits::WrappingAdd<u64>",
            )
            self.assertEqual(install_binding(project, binding)[0], 0)
            generated = project / "vendor/rust-bindings/numbers/ffi/src/lib.rs"
            generated.write_text("// hand edit\n", encoding="utf-8")
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("refusing to overwrite", error)
            forced, _, forced_error = invoke(
                "--project",
                str(project),
                "--binding-root",
                str(binding),
                "--force-generated",
            )
            self.assertEqual((forced, forced_error), (0, ""))
            self.assertIn("#[diplomat::bridge]", generated.read_text(encoding="utf-8"))

        conflicting = HAT_MANIFEST.replace(
            "[dependencies]\n",
            '[dependencies]\n"sample-app-rust-mojo-bindings" = "9"\n',
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root, conflicting)
            binding = write_binding(
                root,
                binding_id="text",
                crate_name="unicode-segmentation",
                ffi_crate="text_ffi",
                mojo_package="text",
                specialization="unicode_segmentation::Graphemes<'static>",
            )
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("Refusing to overwrite a user entry", error)

    def test_closed_world_rejects_untracked_binding_and_aggregate_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="flags",
                crate_name="bitflags",
                ffi_crate="flags_ffi",
                mojo_package="flags",
                specialization="bitflags::Flags<u32>",
            )
            old_mojo = binding / "mojo/flags/old.mojo"
            old_mojo.write_text("# old generated source\n", encoding="utf-8")
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("unexpected source files: old.mojo", error)
            self.assertFalse((project / "vendor").exists())
            old_mojo.unlink()
            old_source = binding / "ffi/obsolete.rs"
            old_source.write_text("// obsolete generated helper\n", encoding="utf-8")
            self.assertEqual(install_binding(project, binding)[0], 0)
            destination = project / "vendor/rust-bindings/flags"
            note = destination / "notes.txt"
            note.write_text("untracked\n", encoding="utf-8")
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("untracked files: notes.txt", error)
            note.unlink()

            cargo_config = project / "vendor/rust-bindings/.cargo/config.toml"
            cargo_config.parent.mkdir()
            cargo_config.write_text("[net]\noffline = true\n", encoding="utf-8")
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("Unknown directory in closed aggregate", error)
            cargo_config.unlink()
            cargo_config.parent.rmdir()

            old_source.unlink()
            result, report, error = invoke(
                "--project", str(project), "--binding-root", str(binding)
            )
            self.assertEqual((result, error), (0, ""))
            assert report is not None
            self.assertIn(
                "vendor/rust-bindings/flags/ffi/obsolete.rs", report["removed"]
            )
            self.assertFalse((destination / "ffi/obsolete.rs").exists())

            staged_config = binding / ".cargo/config.toml"
            staged_config.parent.mkdir()
            staged_config.write_text("[build]\nrustflags = []\n", encoding="utf-8")
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("may not contain .cargo", error)

    def test_stale_aggregate_owned_file_is_safely_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="pattern",
                crate_name="regex",
                ffi_crate="pattern_ffi",
                mojo_package="pattern",
                specialization="regex::Regex",
            )
            self.assertEqual(install_binding(project, binding)[0], 0)
            aggregate = project / "vendor/rust-bindings"
            legacy = aggregate / "legacy-generated.txt"
            legacy.write_text("old aggregate output\n", encoding="utf-8")
            manifest_path = aggregate / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["generated_files"][legacy.name] = integrator.sha256_file(legacy)
            manifest["generated_modes"][legacy.name] = 0o644
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            result, report, error = invoke(
                "--project", str(project), "--binding-root", str(binding)
            )
            self.assertEqual((result, error), (0, ""))
            assert report is not None
            self.assertIn("vendor/rust-bindings/legacy-generated.txt", report["removed"])
            self.assertFalse(legacy.exists())

    def test_build_outputs_paths_tests_and_schema_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="identity",
                crate_name="uuid",
                ffi_crate="identity_ffi",
                mojo_package="identity",
                specialization="uuid::Uuid",
            )
            for relative in (
                "ffi/target/release/leak.txt",
                ".pixi/env/leak.txt",
                "artifacts/leak.txt",
            ):
                path = binding / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("do not copy\n", encoding="utf-8")
            result, _, error = install_binding(project, binding)
            self.assertEqual((result, error), (0, ""))
            destination = project / "vendor/rust-bindings/identity"
            self.assertFalse((destination / "ffi/target").exists())
            self.assertFalse((destination / ".pixi").exists())
            self.assertFalse((destination / "artifacts").exists())

            test_file = binding / "ffi/tests/bridge.rs"
            test_file.write_text("fn helper() {}\n", encoding="utf-8")
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("explicit #[test]", error)

            project_before = {
                path.relative_to(project).as_posix(): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file()
            }
            test_file.write_text(
                textwrap.dedent(
                    '''\
                    // #[test]
                    // fn line_comment_fake() {}
                    /* outer /* #[test] fn nested_comment_fake() {} */ */
                    const FAKE: &str = r#"#[test] fn raw_string_fake() {}"#;
                    const ALSO_FAKE: &str = "#[test] fn string_fake() {}";
                    const QUOTE: char = '"';
                    fn lifetime<'a, 'b>() {}
                    fn helper() {}
                    '''
                ),
                encoding="utf-8",
            )
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("explicit #[test]", error)
            self.assertEqual(
                project_before,
                {
                    path.relative_to(project).as_posix(): path.read_bytes()
                    for path in project.rglob("*")
                    if path.is_file()
                },
            )

            test_file.write_text("#[test]\nfn works() {}\n", encoding="utf-8")
            manifest = binding / "binding.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'cargo_manifest = "ffi/Cargo.toml"',
                    'cargo_manifest = "../Cargo.toml"',
                ),
                encoding="utf-8",
            )
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("unsafe path component", error)

    def test_cargo_graph_missing_wrong_and_external_dependencies_are_refused(self) -> None:
        cases = {
            "missing upstream": (
                lambda text: text.replace(
                    '[dependencies]\n"serde" = { version = "=2.3.4", default-features = true, features = [] }\n',
                    "[dependencies]\n",
                ),
                "must directly depend",
            ),
            "wrong exact version": (
                lambda text: text.replace('version = "=2.3.4"', 'version = "=2.3.5"', 1),
                "version must exactly match",
            ),
            "wrong features": (
                lambda text: text.replace("features = []", 'features = ["derive"]'),
                "features do not match",
            ),
            "external path": (
                lambda text: text
                + '\n[dev-dependencies]\nhelper = { path = "../../../outside" }\n',
                "external path dependency",
            ),
            "wrong library name": (
                lambda text: text.replace('name = "serde_ffi"\ncrate-type', 'name = "other_ffi"\ncrate-type'),
                "builds library",
            ),
            "missing cdylib": (
                lambda text: text.replace('["cdylib", "rlib"]', '["rlib"]'),
                'must include "cdylib"',
            ),
        }
        for label, (mutation, expected) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = write_project(root)
                binding = write_binding(
                    root,
                    binding_id="serde_wrap",
                    crate_name="serde",
                    ffi_crate="serde_ffi",
                    mojo_package="serde_wrap",
                    specialization="serde::Serializer<u8>",
                )
                cargo = binding / "ffi/Cargo.toml"
                cargo.write_text(mutation(cargo.read_text(encoding="utf-8")), encoding="utf-8")
                refused, _, error = install_binding(project, binding)
                self.assertEqual(refused, 2)
                self.assertIn(expected, error)

    def test_required_source_and_ffi_test_schema_is_closed(self) -> None:
        cases = {
            "exact version": (
                lambda text: text.replace('version = "=2.3.4"', 'version = "2.3.4"', 1),
                "exact Cargo version",
            ),
            "source kind": (
                lambda text: text.replace('source_kind = "registry"\n', "", 1),
                "[crate].source_kind",
            ),
            "features": (
                lambda text: text.replace("features = []\n", "", 1),
                "[crate].features",
            ),
            "default features": (
                lambda text: text.replace("default_features = true\n", "", 1),
                "[crate].default_features",
            ),
            "registry checksum": (
                lambda text: text.replace(
                    f'checksum = "{fixture_checksum("schema-crate")}"',
                    'checksum = "ABC123"',
                    1,
                ),
                "64 lowercase hexadecimal",
            ),
            "ffi tests": (
                lambda text: text.replace(
                    'tests = ["ffi/tests/bridge.rs"]', "tests = []", 1
                ),
                "[ffi].tests must be a non-empty",
            ),
        }
        for label, (mutation, expected) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = write_project(root)
                binding = write_binding(
                    root,
                    binding_id="schema_wrap",
                    crate_name="schema-crate",
                    ffi_crate="schema_ffi",
                    mojo_package="schema_wrap",
                    specialization="schema_crate::Value<u8>",
                )
                manifest = binding / "binding.toml"
                manifest.write_text(
                    mutation(manifest.read_text(encoding="utf-8")), encoding="utf-8"
                )
                refused, _, error = invoke(
                    "--project", str(project), "--binding-root", str(binding)
                )
                self.assertEqual(refused, 2)
                self.assertIn(expected, error)

    def test_checksum_source_features_and_default_features_bind_resolution_to_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="encoding",
                crate_name="base64",
                ffi_crate="encoding_ffi",
                mojo_package="encoding",
                specialization="base64::Engine<Vec<u8>>",
                features=("alloc",),
                default_features=False,
            )
            lock = binding / "ffi/Cargo.lock"
            original_lock = lock.read_text(encoding="utf-8")
            lock.write_text(
                original_lock.replace(fixture_checksum("base64"), "f" * 64),
                encoding="utf-8",
            )
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("checksum does not match", error)
            lock.write_text(original_lock, encoding="utf-8")

            resolution = json.loads(resolution_for(binding))
            resolution["request"]["features"] = []
            refused, _, error = invoke(
                "--project", str(project), "--binding-root", str(binding),
                "--resolution", json.dumps(resolution),
            )
            self.assertEqual(refused, 2)
            self.assertIn("request.features", error)
            resolution = json.loads(resolution_for(binding))
            resolution["request"]["default_features"] = True
            refused, _, error = invoke(
                "--project", str(project), "--binding-root", str(binding),
                "--resolution", json.dumps(resolution),
            )
            self.assertEqual(refused, 2)
            self.assertIn("request.default_features", error)
            resolution = json.loads(resolution_for(binding))
            resolution["resolved"]["source"] = "registry+https://wrong.invalid/index"
            refused, _, error = invoke(
                "--project", str(project), "--binding-root", str(binding),
                "--resolution", json.dumps(resolution),
            )
            self.assertEqual(refused, 2)
            self.assertIn("does not match the upstream package source", error)

    def test_git_identity_and_full_source_request_are_checked_on_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="git_codec",
                crate_name="git-codec",
                ffi_crate="git_codec_ffi",
                mojo_package="git_codec",
                specialization="git_codec::Codec<u8>",
                source_kind="git",
            )
            self.assertEqual(install_binding(project, binding)[0], 0)
            manifest = binding / "binding.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "features = []", 'features = ["simd"]'
                ),
                encoding="utf-8",
            )
            cargo = binding / "ffi/Cargo.toml"
            cargo.write_text(
                cargo.read_text(encoding="utf-8").replace(
                    "features = []", 'features = ["simd"]'
                ),
                encoding="utf-8",
            )
            refused, _, error = invoke(
                "--project", str(project), "--binding-root", str(binding)
            )
            self.assertEqual(refused, 2)
            self.assertIn("no longer matches binding.toml", error)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="git_time",
                crate_name="git-time",
                ffi_crate="git_time_ffi",
                mojo_package="git_time",
                specialization="git_time::Clock",
                source_kind="git",
            )
            lock = binding / "ffi/Cargo.lock"
            lock.write_text(
                lock.read_text().replace(GIT_REV, "f" * 40), encoding="utf-8"
            )
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("Git URL/commit do not match", error)

    def test_modified_aggregate_recipe_is_refused_and_local_paths_are_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="pattern",
                crate_name="regex",
                ffi_crate="pattern_ffi",
                mojo_package="pattern",
                specialization="regex::Regex",
            )
            self.assertEqual(install_binding(project, binding)[0], 0)
            recipe = project / "vendor/rust-bindings/recipe.yaml"
            recipe.write_text("# hand-written replacement\n", encoding="utf-8")
            refused, _, error = install_binding(project, binding)
            self.assertEqual(refused, 2)
            self.assertIn("aggregate package files were edited", error)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            binding = write_binding(
                root,
                binding_id="local",
                crate_name="local-crate",
                ffi_crate="local_ffi",
                mojo_package="local_wrap",
                specialization="local_crate::Thing",
            )
            manifest = binding / "binding.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'source_kind = "registry"', 'source_kind = "path"'
                ),
                encoding="utf-8",
            )
            refused, _, error = invoke(
                "--project", str(project), "--binding-root", str(binding)
            )
            self.assertEqual(refused, 2)
            self.assertIn("source snapshot policy", error)


if __name__ == "__main__":
    unittest.main()
