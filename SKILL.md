---
name: bind-rust-to-mojo
description: Generate, integrate, build, and test a new Rust-to-Mojo binding for an exact crates.io, named-private-registry, or immutable Git Rust crate in either an existing Pixi Mojo project or a new standalone pixi-build-mojo library repository; local crates may be inspected but packaging is currently deferred. Use for Rust APIs that must become ordinary Mojo imports, including APIs needing explicit generic specialization or semantic FFI adaptation. Do not use for Mojo-to-Rust bindings.
---

# Bind Rust to Mojo

Create a binding for the crate the user requested. Inspect that crate and
generate a new semantic FFI projection; do not select a checked example or
rename an earlier generated binding.

This tooling is experimental. The crate-agnostic path has been exercised on
multiple unrelated crates, but the Diplomat HIR backend and semantic projection
may still need implementation work for a new Rust API shape. Do not call a run
successful until that invocation's newly generated Rust and Mojo tests pass.

## Invariants

- Work in exactly one target mode: preserve the existing Pixi Mojo project
  supplied by the user, or create a standalone library only when the user asks
  for one or clearly supplies an otherwise-empty target for reusable output.
- Never modify the upstream Rust crate.
- Resolve the exact requested source with `cargo metadata` before inspecting or
  generating from it.
- Generate a companion Rust crate with a stable C ABI and a separate ergonomic
  Mojo package.
- Never copy, rename, substitute into, or install a crate-specific fixture,
  reference bridge, or previously generated output. Test fixtures are evidence,
  not production inputs.
- Classify each relevant public API item as `DIRECT`, `ADAPTED`,
  `MONOMORPHIZED`, `OPAQUE`, or `SKIPPED`; record a reason for every skip.
- Never make a final choice for an unresolved Rust generic. Reuse a compatible
  `binding.toml` choice, accept the user’s explicit choice, or ask the user.
- Ask before an FFI adaptation that materially changes observable semantics.
- Never expose native Rust layout unless the generated representation is
  independently FFI-safe. Catch Rust panics before they cross the C ABI.
- Treat the manifest's closed, exact `[tools]` table as a verified claim: bind
  it to the ABI report, companion Cargo graph, and target Mojo compiler rather
  than copying version strings without checking them.
- Preserve existing Pixi configuration. In standalone mode, create only the
  reviewed generic repository shell before integration. Let Pixi update
  `pixi.lock` in either mode.
- Do not vendor build products, temporary source trees, or environment-specific
  paths.

For Mojo source, use the installed `mojo-syntax` skill and compile against the
project’s pinned Mojo version. Consult current official Pixi documentation and
the installed Pixi CLI for package/build syntax rather than relying on recalled
commands.

Read [Decision 0001](plans/decisions/0001-general-purpose-generation.md) when
changing generation boundaries or considering reuse of checked output. Read
[Decision 0002](plans/decisions/0002-pixi-artifact-command.md) when selecting a
Pixi artifact workflow. Read
[Decision 0003](plans/decisions/0003-out-of-tree-diplomat-backend.md) before
changing the Diplomat/backend boundary. Read
[Decision 0004](plans/decisions/0004-semantic-projection-source.md) before
changing the regeneration or semantic-source boundary. Read
[Decision 0005](plans/decisions/0005-private-registry-and-vendored-upstream.md)
when the crate lives in a private or alternate Cargo registry. Read
[Decision 0006](plans/decisions/0006-global-runtime-state.md) before changing
how wrappers load the library or resolve symbols. Read the rust-lapper files under
`tests/acceptance/` only when running that explicit acceptance scenario. Do not
load `original_plan.md` during ordinary binding work; it is historical
and contains superseded vertical-slice choices.

Set `<skill-root>` to the directory containing this `SKILL.md`. Read the
[binding manifest schema](references/binding-manifest.md) before generating or
regenerating a binding.

## Target modes

Choose exactly one mode before changing the target:

- **Existing project:** use the workflow below unchanged when the user supplies
  a compatible project with `pixi.toml`.
- **Standalone library:** when the user requests a reusable library repository,
  read [the standalone-library workflow](references/standalone-library.md) in
  full. Resolve the requested Rust source, scaffold the otherwise-empty target
  with `scripts/scaffold_standalone_library.py`, then run the same semantic
  binding workflow below and complete the standalone productization contract.

Creating a local repository shell does not authorize choosing a license,
creating a hosted repository, changing repository settings, adding a remote,
pushing, opening a pull request, or enabling Pages. Do those only when the user
requests them. Repository coordinates may be used to render hosted CI and docs
configuration when they are already known.

## Workflow

### 1. Validate the target

Record the initial working-tree state. In existing-project mode, locate the
project root and verify that it contains `pixi.toml` and a Mojo package shape
compatible with its pinned tools. In standalone mode, first follow the
standalone reference to resolve the source, collect the required naming and
compiler inputs, and create the compatible root package; do not run the
scaffolder over an existing project. Require one unambiguous exact Mojo compiler
constraint so generated `.mojoc` files use the same compiler as the root
package. Support Linux and Apple silicon macOS; clearly distinguish platforms
actually tested from platforms only generated for.

### 2. Resolve the source

Accept exactly one source form:

- crates.io package plus exact version;
- Git URL plus immutable revision;
- local crate path.

Resolve all three forms with the bundled resolver and save its JSON outside the
target repository. Representative invocations are:

```text
python3 <skill-root>/scripts/resolve_rust_crate.py --registry NAME@VERSION
python3 <skill-root>/scripts/resolve_rust_crate.py --registry NAME@VERSION --registry-name REGISTRY --registry-index INDEX_URL
python3 <skill-root>/scripts/resolve_rust_crate.py --git URL --rev FULL_COMMIT --package NAME
python3 <skill-root>/scripts/resolve_rust_crate.py --path PATH [--package NAME]
```

An alternate registry is common for corporate crates whose Git mirrors are
private; check `~/.cargo/credentials` and sibling projects' `.cargo/config.toml`
for the registry name and index URL, and prefer the registry (with its
checksum) over a private Git URL as the source of record. Private sources
additionally require the vendored upstream copy described in Decision 0005,
because Pixi package builds redirect `HOME`/`CARGO_HOME` and therefore have no
credentials.

Pass each requested Cargo feature with `--feature`; use
`--no-default-features` only when requested. Record the checksum or immutable
revision/fingerprint, dependency graph, and resolved source path. A local crate
must ultimately be self-contained in the package source graph; stop with a
packaging limitation if it cannot be made so safely.

### 3. Design the projection with the user

Inspect public source, rustdoc, examples, and tests. If the user did not name an
API scope, ask what types and operations matter before treating the entire
public surface as requested. State how feature-gated items and trait APIs enter
the scope. Produce an export plan and identify generic parameters and semantic
mismatches.

For every generic specialization:

1. reuse a compatible value already recorded in `binding.toml`;
2. otherwise use a concrete type explicitly supplied in the current request;
3. otherwise explain the relevant evidence and ask the user to choose.

Examples can support a recommendation but cannot provide consent. Do not
generate Cartesian products of plausible types.

Ask for concrete exposed type, const, and lifetime-policy choices, and for any
narrowing of a generic public method. Do not ask about an incidental generic
container or iterator type used only to implement a Mojo shape the user already
approved.

Prefer adaptations that preserve behavior: opaque handles for layout-unsafe
objects, explicit value conversions for structs, named structs for tuple
returns, UTF-8 representations for strings, and explicit error APIs for
fallible functions. Prefer lazy Rust-backed Mojo iterators when a Rust iterator
can safely expose `next`; materialize a collection only when required and with
the user’s agreement if semantics or performance materially change.

For a fallible operation whose error message matters (an open or parse), the
scalar-only ABI cannot raise with dynamic text; project an opaque result
carrier with `is_ok`, a UTF-8 error accessor, and a single-shot value take.
Choose Optional versus raising deliberately: Mojo forbids mutating methods on
rvalues, so a chained `.take()` on an Optional return does not compile and
callers must bind an lvalue first. Where `None` is a normal outcome (a map
lookup, an indexed access) return Optional; where `None` means the caller
broke the contract (taking a value twice, taking after a failed open), map it
with `optional_none_error` so the getter returns the value directly and
raises instead.

For "expose every field" requests over a large struct tree, project one
read-only view opaque per struct: each view retains a shared handle on the
root parse (for example an `Arc` of the parsed file) plus a raw node pointer,
so views stay valid independently of parent-view lifetimes without copying.
Project a payload enum as a fieldless kind discriminant plus per-variant
payload accessors that return Optional views or Optional payloads.

Mojo 1.0 does support process-wide runtime globals through `std.ffi._Global`
(the same mechanism `std.python` uses for the CPython interpreter handle).
The wrapper generator opens the shared library once into such a global and
resolves every C function pointer exactly once into fields of that global
state, CPython-style, so a wrapper call costs one named-global lookup plus a
field read (~10 ns) with no per-call `dlsym`, no per-object `dlopen`, and no
allocation. These are underscore-private stdlib APIs; re-verify them against
the target compiler before relying on them for a new Mojo version.

### 4. Write the semantic inputs

Create the complete binding in a temporary staging directory outside the target
project:

```text
<staging>/<binding-id>/binding.toml
```

For a new binding, derive both `binding.toml` and `ffi/src/lib.rs` from the exact
resolved crate; never seed either from another binding. For an unchanged
binding, copy its own reviewed manifest and bridge into staging, validate them
against the newly resolved Cargo graph and each other, and preserve both
byte-for-byte. Do not ask an agent to resynthesize an unchanged bridge.

If source identity, features, requested scope, specialization, or adaptation
changed, re-inspect the upstream crate, revise the manifest and bridge
explicitly, then regenerate the mechanical ABI and Mojo outputs. The manifest
is the audit record; the bridge is the executable implementation of those
decisions. Neither is a reusable template for another crate.

Record exact source identity and features, package names, tool versions,
user-approved specializations and adaptations, plus every export status and
reason. Keep it deterministic: omit timestamps, usernames, temporary paths,
and environment prefixes.

Write exactly the six required `[tools]` keys documented in the manifest
schema. Use bare exact semver-like values. The ABI backend report supplies the
backend/core evidence; the companion Cargo graph and target Pixi manifest must
independently agree with the remaining build-tool claims.

### 5. Create or validate the Rust bridge

Generate the companion crate at:

```text
<staging>/<binding-id>/ffi/
```

For a new or semantically changed binding, write one self-contained source file
with an inline `#[diplomat::bridge]` module, derived from the resolved API and
manifest. For an unchanged binding, validate the preserved file instead. The
current backend does not inline an external `mod ffi;` tree.
Do not place `use std::...` or `use core::...` declarations inside the bridge:
the backend rejects those as non-FFI type imports. Use fully qualified standard
library paths within bridge helper bodies instead.
Generate deterministic, crate-namespaced C symbols. Use opaque ownership
handles, explicit destructors, panic barriers, and status/out-parameter returns
where appropriate. Ensure iterator state and owner mutation cannot invalidate
each other; `next` must not return a Rust reference whose lifetime escapes the
call.

With the pinned Diplomat 0.16.1 macros, put the default namespaced
`#[diplomat::abi_rename = "rust_mojo__<binding>__{0}"]` on the bridge module.
In particular, do not rely on a function-level `abi_rename` for a free
function: the HIR accepts that spelling but the proc macro can leave the
attribute behind and make the companion crate fail to compile. Method-level
overrides remain available when the inherited name would collide. An opaque
type with any `&mut self` method must be declared `#[diplomat::opaque_mut]`,
not `#[diplomat::opaque]`, or HIR lowering fails.

Strings, options, and bulk numeric access do not need new ABI shapes: keep
the raw layer scalar/pointer-only and use the wrapper generator's closed
projections instead. A string crosses as a `<field>_utf8_len` /
`<field>_utf8_copy(buf_addr, buf_len)` pair (`string_copy`); an optional
scalar, enum, or string pairs its getter with a boolean `has_` companion
(`optional_via`) to become a public Mojo `Optional`; a numeric collection
exposes `len` plus a `fill` function taking a caller-owned mutable span
(`mutable-primitive-slice`). The manifest reference documents all three.

Format and test the bridge with an external temporary `CARGO_TARGET_DIR` so no
`target/` appears in vendored source.

Declare `diplomat` and `diplomat-runtime` as non-optional root dependencies at
the exact versions recorded in `[tools]`. Generate `Cargo.lock` normally, then
verify its companion root resolves unambiguously to the selected registry
`diplomat` and `diplomat-runtime` packages and that the selected `diplomat`
resolves to the recorded `diplomat_core`. Allow unrelated versions elsewhere
in an upstream dependency graph. Keep this audit in addition to the exact
upstream crate source/checksum/feature audit.

Give every generated source file a deterministic header naming the resolved
crate/version, manifest, generator, Diplomat, and Mojo versions. Generate
cross-language size, alignment, and field-offset tests for each value struct.
With Mojo 1.0, compute field offsets from runtime pointer-address differences;
`std.reflection.struct_fields.offset_of` is not available in that toolchain.

### 6. Generate raw and ergonomic Mojo layers

Run the experimental `diplomat-gen-mojo` backend over the newly generated
bridge. Extend the backend when the requested Diplomat HIR is expressible but
not yet implemented. If it cannot safely represent an API, report that item as
unsupported instead of substituting code from an acceptance fixture.

```text
CARGO_TARGET_DIR=<temporary-target> cargo run --locked \
  --manifest-path <skill-root>/crates/diplomat-gen-mojo/Cargo.toml -- \
  <staging>/<binding-id>/ffi/src/lib.rs \
  --output <staging>/<binding-id>/mojo/<mojo-package>/_ffi.mojo \
  --report <staging>/<binding-id>/abi-report.json \
  --deny-unsupported
```

Keep layers separate:

```text
<staging>/<binding-id>/mojo/<mojo-package>/
  __init__.mojo
  _ffi.mojo
  _runtime.mojo
  _types.mojo
  _wrappers.mojo
```

The raw layer must mirror the exact C ABI. The current backend emits a schema
and signature layer, so prove each ABI shape with real Rust/Mojo call and layout
tests before treating it as exact. The public layer supplies move-only RAII
ownership, value conversions, error handling, and idiomatic lazy iteration.
For Mojo 1.0.0 dynamic loading, keep every call boundary to scalars and
pointers: scalarize a slice as a private address-plus-length pair, pass value
structs by pointer, and return aggregate values through caller-owned out
pointers. Reject an ABI that passes or returns a C aggregate by value even if it
type-checks; real calls can be mislowered. Wrappers resolve every symbol once
into the process-global runtime state with `_get_function[symbol, alias]`,
typed by the raw layer's `def(...) thin abi("C")` aliases (Decision 0006), so
calls are fully typed against the declared ABI rather than inferred.
This also excludes Diplomat's two-word owned-slice carrier. Project an owned
collection as an opaque Rust owner with scalar/pointer accessors and a generated
destructor instead of wrapping `DiplomatOwnedSlice` directly.
On a later compiler with an explicit dynamic function type, declare `abi("C")`
and add a real ABI regression before relaxing this rule. Resolved function
pointers must not outlive their `OwnedDLHandle`.

Compile a minimal loader probe with the target project's pinned compiler
before emitting the full wrapper: exercise `std.ffi._Global`,
`OwnedDLHandle._get_function[symbol, alias]`, and one real call through a
resolved pointer, since these underscore-private APIs and the dynamic-loading
syntax have changed between Mojo releases. Follow the installed compiler and
`mojo-syntax` skill, not an example written for a different version.

Then run the closed semantic-wrapper generator. It requires an explicit
`[[mojo.types]]` entry for every ABI struct, enum, and opaque type, and an
explicit `[[mojo.functions]]` entry for every ABI function; internal wire
carriers must be marked `skip` with a reason rather than disappearing.

```text
python3 <skill-root>/scripts/generate_mojo_package.py \
  --binding <staging>/<binding-id>/binding.toml \
  --report <staging>/<binding-id>/abi-report.json \
  --output-dir <staging>/<binding-id>/mojo/<mojo-package>
```

This creates `_runtime.mojo`, `_types.mojo`, `_wrappers.mojo`, and
`__init__.mojo`, validates every report-declared symbol and exact function
signature against `_ffi.mojo`, and replaces only the raw file's backend header
with the complete deterministic source/manifest/tool provenance header. Pass
`--ffi <path>` when the raw file is not already at the output path. Treat any
manifest/report mismatch as a semantic error; do not infer a public role from
an ABI name. Rerun the command with `--check` during the idempotency proof.

Do not make `CONDA_PREFIX` the only runtime lookup. Prefer executable- or
loader-relative lookup for the installed `$PREFIX/lib` library, with an
explicit override and environment-prefix lookup only as development fallbacks.
For Linux, resolve the real executable through `/proc/self/exe`; for macOS, use
`_NSGetExecutablePath` and canonicalize it. Also canonicalize `argv[0]` or its
`PATH` match as a fallback so basename and symlink launches do not make library
lookup relative to the current working directory.

### 7. Integrate with Pixi generically

Use `scripts/integrate_binding.py` to integrate the paths and metadata described
by this invocation’s `binding.toml`. The integrator may perform deterministic
Pixi/package mutations, but it must not contain crate names, APIs, bridge
sources, Mojo wrappers, or fixture selection logic.

Before integration, require the project's one exact Mojo compiler constraint,
normalizing either `=VERSION` or `==VERSION`, to equal
`[tools].mojo_version`. The integrator rechecks this, the closed tool table, and
the Cargo manifest/lock provenance before it mutates the project; treat a
failure as a stale or dishonest binding input and regenerate or align the
project rather than bypassing the check.

The integrator also reruns the semantic wrapper generator in `--check` mode.
Missing or edited `abi-report.json`, raw ABI drift, incomplete export mappings,
stale wrappers, and unexpected `.mojo` modules must therefore fail before any
project mutation.

```text
python3 <skill-root>/scripts/integrate_binding.py \
  --project <project-root> \
  --binding-root <staging>/<binding-id> \
  --resolution @<resolution.json> \
  --artifact-mode <build-or-publish>
```

Select the mode from the project-compatible Pixi executable's actual help:
`build` for the older `pixi build --output-dir` workflow and `publish` for the
newer multi-package `pixi publish --target-dir` workflow. The integrator's
`requires-pixi` inference is a conservative fallback, not a substitute for
checking the selected executable.

Generate a project-scoped bindings source package under
`vendor/rust-bindings/`. Build companion crates with Cargo’s locked release
mode, install shared libraries below `$PREFIX/lib`, and precompile Mojo packages
below `$PREFIX/lib/mojo`. Preserve the target project’s existing backend and
configuration. Package correctness must not depend on convenience Pixi tasks.

Package builds are hermetic: rattler-build redirects `HOME` and `CARGO_HOME`
into the build tree, so no git credentials, keychain, cargo token, or user
cargo config exist inside the recipe. Anonymous crates.io fetches still work;
any private index or private Git fetch fails there even when it works in the
developer shell. For a named-registry crate the integrator emits the registry
definition as Cargo environment configuration in the recipe commands, and a
private crate must additionally ship the checksum-verified
`ffi/vendored/<crate>/` copy wired in through build-time source replacement
(Decision 0005).

### 8. Build, test, and repair

Let the project-compatible Pixi executable update its lockfile, build source
packages, and install the generated binding. Inspect `pixi build --help` and
`pixi publish --help`. In `build` mode, export the aggregate dependency first
with `pixi build --manifest-path vendor/rust-bindings/pixi.toml --output-dir
...`, then export the root package with `pixi build --output-dir ...`. In
`publish` mode, use `pixi publish --target-dir ...` to build the workspace in
dependency order. Exercise the reported `artifact_commands` and `pixi install`,
then prove an external prefix can install the complete artifact set. Run:

1. Rust tests for upstream-crate → companion-bridge behavior;
2. Mojo tests for companion-bridge → ergonomic-package behavior.

Each declared Rust integration-test target must contain an actual `#[test]`,
and the packaged build must fail when Cargo lists zero tests; comments or string
literals that merely spell `#[test] fn` are not evidence.

Also verify import without `-I`, ownership/destruction, iterator exhaustion and
early destruction when relevant, panic/error behavior, and direct execution of
an installed binary without `pixi run`. Fix generation or backend gaps within
scope and repeat. If a safe binding remains impossible, stop with a precise
unsupported report; do not present generated-but-uncompiled code as success.

In standalone mode, binding tests alone are insufficient. Finish and exercise
the public facade, representative standalone examples, downstream Git consumer,
strict generated documentation, rendered docs site, and CI/package checks from
the standalone reference before reporting success.

Three Mojo 1.0 tooling traps invalidate careless verification. `-I` does not
shadow a package already installed in the Pixi environment, so after changing
generated wrappers you must reintegrate and `pixi install` before any run
proves anything. The compiler cache reuses binaries for content-identical
source files and prints the first-seen path, so an unchanged test file can
silently run a stale build; trust content changes, not paths. `mojo run`
executes unoptimized code, so measure performance only with `mojo build`
binaries. When benchmarking, expect roughly: raw resolved C call ~1 ns,
generated wrapper call ~10 ns, bulk span fill a few microseconds for tens of
thousands of elements; a wrapper call costing hundreds of nanoseconds means
one of these traps, not FFI overhead.

### 9. Prove regeneration and report

Hash or diff generated inputs, rerun from the same manifest, and require no
source or Pixi diff. Then report source identity, concrete specializations,
export classification counts, adaptations, tested platforms, import/test
commands, and deferred APIs. For a standalone library, also report repository
coordinates if configured, public facade and example coverage, documentation
coverage and executable-example counts, downstream Git installation proof, and
which hosted operations remain intentionally unperformed.

Use the integrator's `--check` mode on the regenerated staging tree; it must
report no changes.

On failure after mutation, retain useful diagnostic source and report the
failing phase, exact error, changed files, lockfile status, and next action.

## Acceptance scenarios

Acceptance cases constrain the general workflow; they do not provide generated
code. The initial case is declared under `tests/acceptance/rust_lapper/` and
must travel through the same resolve → inspect → manifest → bridge → backend →
wrapper → generic integration → test path as every other crate. Standalone-mode
acceptance additionally uses the generic scaffold and completion properties in
`references/standalone-library.md`; a finished standalone repository is product
evidence, never a crate-specific production template.
