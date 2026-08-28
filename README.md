# Rust → Mojo bindings

This repository is an experimental Codex skill for generating bindings from a
Rust crate into an existing Pixi-based Mojo project. The product is deliberately
crate-agnostic: each invocation inspects the requested crate and creates a new
FFI projection for that crate.

The intended result is an ordinary Mojo import backed by generated, vendored
source—without modifying the Rust crate, asking the user to hand-write C glue,
or requiring `mojo -I` at use sites.

## Project status

This is pre-release tooling. In manual development acceptance runs, the same
crate-agnostic source-inspection → new bridge → Diplomat HIR → ergonomic wrapper
→ Pixi package path has been exercised from fresh crates.io sources for both
`rust-lapper` and the unrelated `semver` crate on Linux x86-64. Both produced
real Rust `cdylib` calls and normal Mojo imports; both also passed clean
Hat-style Pixi installation, two-package artifact publication,
independent-prefix installation, and direct execution outside `pixi run`. No
production script selects either crate or copies their proof output. A third
fresh-source check against `strsim` exercised unrelated free functions with
UTF-8 inputs and scalar results through the same backend and wrapper generator.
The repository automates component and contract regressions; the full external
artifact run remains a manual acceptance procedure until a portable harness is
checked in.

That demonstrates the general workflow, not universal Rust API coverage.
Expect Codex to extend or repair the semantic projection or HIR backend when a
new crate needs a Rust shape outside the current scalar/pointer, value,
opaque-owner, scalar-status/out-value, scalarized-input, string-copy,
optional-via-presence, mutable-span-fill, and lazy-iterator surface. A run is successful only when its newly generated Rust and Mojo
integration tests pass; generated files alone do not constitute support.

Mojo 1.0 does provide process-global runtime storage through
`std.ffi._Global`, the mechanism `std.python` uses for the CPython handle.
The wrapper generator opens the shared library once into such a global and
resolves every C function pointer exactly once into fields of that global
state (Decision 0006). Wrapper calls cost one named-global lookup plus a
field read — roughly 10 ns against ~1 ns for the raw C call — with no
per-object `dlopen` and no per-call `dlsym`. `_Global` and
`OwnedDLHandle._get_function` are underscore-private stdlib APIs validated
against Mojo 1.0.0; probe them before trusting a new compiler release.

Linux x86-64 has been executed, and Apple-silicon macOS has been validated
end to end (build, install, publish, external-prefix install) by a real
private-registry binding. Linux AArch64 package branches and lock resolution
are generated but not natively executed.

Exact crates.io, named-alternate-registry (including private registries with
the vendored upstream copy of Decision 0005), and immutable Git resolution are
implemented. Local workspaces can be inspected, but the initial package
integrator must reject a local-path dependency until that source is captured
inside a self-contained package graph; it must not emit a binding that works
only from one checkout.

`rust-lapper` 1.3.0 is a pinned acceptance scenario for developing and
evaluating the general workflow. It is not the default implementation, a list
of supported crates, or a template that may be copied into another project.

## Install the skill

For a local checkout, make the repository discoverable as a personal Codex
skill:

```bash
skill_home="$HOME/.agents/skills"
mkdir -p "$skill_home"
ln -s /absolute/path/to/mojo-bind-rust \
  "$skill_home/bind-rust-to-mojo"
```

For only one Mojo repository, put the same symlink at
`<mojo-project>/.agents/skills/bind-rust-to-mojo` instead.

Codex normally detects the skill automatically; if it does not appear as
`$bind-rust-to-mojo` in `/skills`, start a new session. See the
[Codex skill documentation](https://developers.openai.com/codex/skills) for
the complete discovery rules.

## Bind a Rust dependency

Open Codex in an existing Pixi Mojo project and state the source and Mojo API
you need:

```text
Use $bind-rust-to-mojo to bind <crate>@<exact-version> into this Pixi Mojo
project. Expose <types and operations>. Ask me to choose every unresolved
generic type and any adaptation that could materially change semantics. Build
and test the Rust bridge and Mojo package, integrate them with Pixi, and do not
stop after merely generating files.
```

A Git URL plus immutable revision can replace the crates.io specification.
Local crates can currently be resolved and inspected, but packaging them is an
explicitly deferred case until their source graph can be snapshotted into the
artifact; the skill must not leave a checkout-dependent binding behind. If a
Rust generic needs a concrete specialization and the project has not already
recorded one, the skill must ask rather than choose for you.

For example, these are all intended requests:

```text
Bind semver@1.0.28 and expose Version parsing and precedence comparison.

Bind regex@1.13.1 and expose Regex.new, is_match, find, and a lazy find_iter.

Bind smallvec@1.15.2 and expose construction, push, indexing, and iteration.

Inspect whether the local crate at ../engine-core can be packaged
self-contained before binding Engine and EventStream; stop if it cannot.
```

The regex request may require an ownership decision for its haystack. The
smallvec request must ask for its element type and inline capacity. A local
crate may expose its own unresolved generics. The skill should explain those
decisions rather than assume them.

The production workflow is:

```text
resolve exact Rust source
  → inspect the requested public API
  → ask for unresolved semantic and generic choices
  → write binding.toml and a new companion Rust bridge
  → lower its Diplomat HIR with diplomat-gen-mojo
  → generate ergonomic Mojo ownership/API wrappers
  → integrate the generated packages with integrate_binding.py
  → let Pixi install, build, and test both sides
```

Successful generation should add approximately:

```text
vendor/rust-bindings/
├── pixi.toml
├── recipe.yaml
├── manifest.json
└── <binding-id>/
    ├── binding.toml
    ├── manifest.json
    ├── ffi/
    ├── mojo/<mojo-package>/
    └── tests/
```

Each binding has two reviewed semantic inputs: `binding.toml` records source
identity, user choices, exports, ABI symbols, and wrapper roles, while
`ffi/src/lib.rs` implements the crate-specific projection. The ABI report and
Mojo package are deterministic outputs of those inputs. On an unchanged rerun,
the skill validates and preserves the audited bridge byte-for-byte; a source,
feature, scope, specialization, or adaptation change requires a fresh upstream
inspection and an explicit bridge revision. Generated source and its Cargo
lockfile belong in version control. Rust `target/` directories, shared
libraries, compiled Mojo packages, and environment-specific paths do not.

Crate-specific fixtures and previously generated bindings are test evidence
only. They must never be copied, renamed, or parameter-substituted into a user
project. Every production bridge and wrapper must be derived from the resolved
source and the current binding manifest.

After a successful invocation, `pixi install` rebuilds the binding from a clean
checkout. The skill also exercises the artifact workflow supported by the
project's Pixi version. Older build-mode projects emit the aggregate binding
artifact first and the root artifact second with `pixi build`; newer projects
use `pixi publish --target-dir` to emit the workspace in dependency order. It
then installs the complete artifact set into a separate prefix and verifies
that the binary runs directly without `pixi run`. The installed prefix and its
runtime libraries still need to remain together; this is not a promise of a
single static executable.

## Example request

The repository’s pinned acceptance request can also be used as a concrete
trial from a Hat-style Pixi Mojo project:

```text
Use $bind-rust-to-mojo to bind rust-lapper 1.3.0 into this Pixi Mojo project.
Expose Interval and Lapper, using usize coordinates and u32 values. Preserve
lazy iteration for find, seek, and depth. Generate the binding from the
resolved crate source, integrate it with Pixi, and run the Rust and Mojo tests.
```

This example tests the same general path as any other crate; it does not enable
a rust-lapper-specific installer. The complete declarative request and expected
properties live in [`tests/acceptance/rust_lapper`](tests/acceptance/rust_lapper).

## Design and development

- [SKILL.md](SKILL.md) defines the installed skill’s execution contract.
- [`crates/diplomat-gen-mojo`](crates/diplomat-gen-mojo) is the experimental
  Diplomat HIR backend.
- [`scripts/generate_mojo_package.py`](scripts/generate_mojo_package.py) closes
  the manifest-to-ABI map and emits the ergonomic Mojo ownership/API layer.
- [`scripts/integrate_binding.py`](scripts/integrate_binding.py) performs the
  crate-agnostic Pixi/package integration after revalidating the closed
  manifest → ABI report → raw ABI → wrapper contract.
- [Decision 0001](plans/decisions/0001-general-purpose-generation.md) explains
  why checked crate-specific templates are rejected as production inputs.
- [Decision 0002](plans/decisions/0002-pixi-artifact-command.md) records the
  version-adaptive Pixi artifact workflow.
- [Decision 0003](plans/decisions/0003-out-of-tree-diplomat-backend.md) explains
  the out-of-tree Diplomat HIR backend and the eventual upstreaming boundary.
- [Decision 0004](plans/decisions/0004-semantic-projection-source.md) defines
  the manifest-plus-audited-bridge regeneration boundary.
- [The original plan](original_plan.md) is retained as historical context;
  its template-based vertical-slice allowances are superseded by Decision 0001.
- The integration follows Pixi's
  [Mojo backend](https://pixi.prefix.dev/latest/build/backends/pixi-build-mojo/)
  and [multi-package workspace](https://pixi.prefix.dev/latest/build/workspace/)
  model.
