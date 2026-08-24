# Rust to Mojo Automatic Bindings Skill

Status: historical baseline. Its checked-template allowance is superseded by
[Decision 0001](plans/decisions/0001-general-purpose-generation.md), and its fixed
Pixi artifact commands are superseded by
[Decision 0002](plans/decisions/0002-pixi-artifact-command.md). Production bindings
must be generated from the resolved crate source.

This document is a non-normative historical record of the original product
proposal and its design review. Current decision records and `SKILL.md` govern
implementation. When experiments force another design change, preserve the
reason in a decision record rather than silently rewriting the original text.

## Goal

Build an installable Codex skill that can be invoked from an existing
Pixi-based Mojo project with a Rust crate specification such as:

```text
bind rust crate rust-lapper@1.3.0
```

After the skill completes, the project contains vendored generated Rust FFI
code and vendored generated Mojo bindings. The Pixi package graph contains
everything needed to build and install them, and Mojo can import the package
without manual C glue, direct Cargo commands, loader-path setup, or `mojo -I`:

```mojo
from rust_lapper import Interval, Lapper
```

The first end-to-end reference crate is exactly `rust-lapper 1.3.0` with the
explicit user-selected specialization:

```text
I = usize
T = u32
```

Rerunning the skill with the same crate, version, features, source, and
specializations must produce no diff except a deterministic regeneration of
the same bytes.

## Product shape

There are two cooperating components:

```text
Codex skill
    |
    | source inspection, semantic projection, orchestration, repair
    v
rust-mojo-bindgen + Diplomat Mojo backend
    |
    | deterministic generation and package integration
    v
vendored companion Rust FFI crate
+ vendored Mojo package
+ Pixi source package
```

The Codex skill owns decisions that cannot always be mechanical:

- which public APIs matter and which public APIs exist;
- which generic specializations are required;
- whether a type is represented by value or as an opaque handle;
- how borrowing, iterators, strings, errors, and collections are projected;
- whether an unsupported API can be adapted without changing its semantics;
- whether the binding actually compiles and behaves like the Rust crate.

The generator and backend own deterministic mechanics:

- names, symbol prefixes, and directory layout;
- Diplomat HIR lowering and Mojo ABI declarations;
- ownership wrappers and destructor wiring;
- source-package and recipe generation;
- manifest schema, provenance, and export reports;
- idempotent mutations and generated-file headers.

The vertical slice may contain hand-authored reference templates while the
backend matures. The templates are executable specifications, not a reason to
postpone the working binding.

> Superseded by Decision 0001: no checked crate-specific template may be a
> production input. Acceptance cases are declarative only.

## Locked design decisions

### Diplomat integration

Implement a real out-of-tree Mojo backend over `diplomat_core` HIR. Do not
write a second Rust parser and do not begin by modifying the upstream Diplomat
repository. Pin the first implementation to:

```text
diplomat          = 0.16.1
diplomat_core     = 0.16.1
diplomat-runtime  = 0.16.0
```

The exact compatible dependency resolution belongs in Cargo.lock. Reconsider
upstreaming the backend only after rust-lapper and a second unrelated crate are
working. The tradeoff is deliberate:

- an out-of-tree backend gives this project release control and a fast repair
  loop;
- using Diplomat HIR still shares its validated FFI type model and avoids a
  parallel parser;
- upstream support later reduces long-term drift, but accepting that coupling
  before the backend has a proven shape would slow the vertical slice.

### Generic specializations

Never silently choose a concrete type for an unresolved generic. Resolve a
specialization in this order:

1. reuse the exact choice in an existing `binding.toml`;
2. use a type explicitly supplied in the current invocation;
3. otherwise ask the user.

Project references, examples, aliases, and documentation may be presented as
evidence or suggested defaults, but they do not authorize a choice. Do not
generate Cartesian products of candidate types.

### Iterators

Do not materialize iterator APIs into owned slices by default. Generate lazy,
Rust-backed Mojo iterators whose `next` operation crosses the C ABI once per
item.

For rust-lapper, a naïve erased pointer to the upstream borrowing iterator is
not sound: Mojo origins keep the owner alive but do not prevent calls to
mutating methods while that iterator exists. A counter checked from an
overlapping Rust `&mut self` is also too late, because forming the alias can
already violate Rust's rules.

Use an interior-mutability lease design for the reference bridge:

```text
Lapper
  -> Arc<SharedState>

SharedState
  -> Mutex<State>

State
  -> rust_lapper::Lapper<usize, u32>
  -> active iterator lease count
  -> poison/error state

FindIterator / DepthIterator / IntervalIterator / SeekIterator
  -> Arc<SharedState>
  -> query and cursor state owned by Rust
  -> one active lease
```

Iterator creation increments the lease exactly once. `next` locks briefly,
computes and copies one FFI-safe item, and releases the lock. Exhaustion,
explicit `close()`, or destruction releases the lease exactly once. A mutator
uses interior mutation and fails while a lease is active with this user-facing
message:

```text
Cannot mutate this Lapper while a Rust-backed iterator is active; exhaust it,
call `close()`, or let it leave scope.
```

No reference into the Rust `Vec` crosses a call boundary. Immutable Rust types
that do not have mutation invalidation may use a simpler wrapper around their
actual iterator.

A normal Mojo iterator implements `Iterator` and `IterableOwned`, and
`__next__()` raises `StopIteration` only for exhaustion. Therefore its Rust
step must otherwise be total. A generally fallible Rust stream must expose an
explicit fallible API instead of disguising errors as end-of-stream.

`seek` uses Rust-owned query state and does not borrow a Mojo `Cursor`. The
cursor represents the reusable starting offset; it can be reused immediately
after iterator construction.

### Panic and error policy

No Rust panic may unwind across the C ABI. Catch panics at generated boundaries
and map them to a stable binding error. Preserve distinct status values for at
least:

- success;
- iterator exhaustion;
- active-iterator mutation conflict;
- invalid input;
- caught Rust panic / poisoned state;
- internal binding failure.

Prefer status plus out-parameter lowering when aggregate returns are unreliable
through Mojo dynamic FFI. Keep the human-readable last-error payload owned by
the Rust handle or an explicitly managed error object; do not expose a dangling
panic string.

### Platform and toolchain baseline

Pin Mojo `1.0.0` for the vertical slice. Support the current Hat project shapes
for Mojo binaries and libraries.

Generate native package logic for:

- `linux-64`;
- `linux-aarch64`;
- `osx-arm64`.

Validate Linux x86-64 in automation first. Validate Apple silicon on the
user's Mac before claiming macOS completion. WSL follows the Linux design. Do
not claim cross-compilation in v1.

### Pixi build and distribution

The command spellings in this historical section are superseded by
[Decision 0002](plans/decisions/0002-pixi-artifact-command.md).

Use Pixi's multi-package build graph rather than development-only tasks as the
source of truth. With the current Pixi release, `pixi install` builds source
packages automatically; distributable artifacts are produced with:

```text
pixi publish --target-dir dist
```

or, for the full dependency set:

```text
pixi publish --target-channel ./dist-channel
```

Keep the Hat root package on `pixi-build-mojo`. Generate one project-scoped
aggregate bindings source package below `vendor/rust-bindings/`, backed by
`pixi-build-rattler-build`, because the Mojo backend does not provide arbitrary
Cargo build hooks.

The root package depends on the generated bindings package at build time so
the `.mojoc` is visible to the Mojo compiler, and at run time so the Rust shared
library is installed with the finished program. Use the dependency section
required by the current Pixi schema and validate it empirically; do not retain
obsolete `host-dependencies` spellings from older proposals.

The generated bindings recipe:

1. runs `cargo build --release --locked` for each companion crate;
2. installs `.so` or `.dylib` files under `$PREFIX/lib`;
3. precompiles each generated Mojo package;
4. installs `.mojoc` files under `$PREFIX/lib/mojo`;
5. tests the installed package, not only the source tree.

Both root and child packages must be publishable for a workspace publish. If a
project explicitly sets `publish = false`, preserve that user decision and
report why artifact publication is unavailable.

The desired clean-checkout development command is simply:

```text
pixi install
```

No binding generator needs to be installed in the target project for this
command to work; all generated build inputs are vendored.

### Running outside `pixi run`

The installed executable must run directly without activation or `pixi run`,
provided its installation prefix remains intact. A single copied executable is
not promised: it still depends on Mojo runtime libraries and the generated Rust
library. Use relative loader paths and package relocation:

```text
Linux: $ORIGIN/../lib
macOS: @executable_path/../lib and/or @rpath
```

The generated Rust-library lookup order is:

1. an explicit binding-specific environment override;
2. a path relative to the running executable/prefix;
3. platform loader resolution by library name;
4. `$CONDA_PREFIX/lib` as a development fallback.

`CONDA_PREFIX` must not be the only runtime strategy. For a portable whole
environment, recommend a Pixi package/channel or `pixi-pack` rather than a
loose executable.

## Core design rule

Never alter the upstream Rust crate. Generate a companion crate:

```text
rust-lapper
    ^ Rust dependency
    |
rust_lapper_mojo_ffi
    | explicit Diplomat bridge / stable C ABI
    v
generated Mojo package rust_lapper
```

This supports crates.io packages, Git revisions, and local paths while keeping
the stable contract at the generated C ABI rather than either language's
native ABI.

## Generated project layout

Given a Hat-style Pixi Mojo project, generate approximately:

```text
my-mojo-project/
|-- pixi.toml
|-- pixi.lock
|-- src/
|   `-- main.mojo
|-- vendor/
|   `-- rust-bindings/
|       |-- pixi.toml
|       |-- recipe.yaml
|       `-- rust_lapper/
|           |-- binding.toml
|           |-- manifest.json
|           |-- ffi/
|           |   |-- Cargo.toml
|           |   |-- Cargo.lock
|           |   `-- src/lib.rs
|           `-- mojo/
|               `-- rust_lapper/
|                   |-- __init__.mojo
|                   |-- _ffi.mojo
|                   |-- _iterator.mojo
|                   `-- lapper.mojo
`-- tests/
    `-- rust_lapper_binding.mojo
```

Everything below `vendor/rust-bindings/rust_lapper/` that is needed to rebuild
is checked in. Rust `target` directories and built `.so`/`.dylib` files are not.

## Binding manifest and provenance

`binding.toml` is the human-editable source of truth after initial generation.
It must include:

```toml
[crate]
name = "rust-lapper"
version = "=1.3.0"
source = "registry"
features = []
rust_module = "rust_lapper"
mojo_package = "rust_lapper"

[ffi]
crate_name = "rust_lapper_mojo_ffi"
diplomat_version = "=0.16.1"
diplomat_runtime_version = "=0.16.0"

[[specializations]]
rust_type = "rust_lapper::Lapper"
mojo_name = "Lapper"
I = "usize"
T = "u32"

[adaptations]
find = "lazy-rust-iterator"
iter = "lazy-rust-iterator"
depth = "lazy-rust-iterator"
seek = "opaque-state+lazy-rust-iterator"
union_and_intersect = "named-struct"
```

It also contains one entry for every discovered public export with a status of:

```text
DIRECT
ADAPTED
MONOMORPHIZED
OPAQUE
SKIPPED
```

Every `SKIPPED` entry needs a precise reason. Never silently omit a public API.
Completeness covers resolved public inherent types, methods, fields, and
selected trait behavior. The report must state its trait-selection policy so
“all public API” is not ambiguous.

`manifest.json` is deterministic machine-readable provenance and records:

- source kind and canonical source identifier;
- exact crate version and registry checksum, Git commit, or normalized local
  path fingerprint;
- features and target triple;
- Cargo.lock hash;
- generator, Diplomat, and Mojo versions;
- generated-file hashes and export report.

Do not include timestamps, temporary paths, usernames, absolute build prefixes,
or other host-specific data in equality-sensitive checked-in files.

## API discovery

Use `cargo metadata` first to resolve the source, version, features, package
location, and dependency graph. Inspect the resolved source, rustdoc, examples,
and tests for the semantic export plan.

Rustdoc JSON remains optional because it is unstable. If it is later used, pin
the nightly toolchain and validate the JSON format version explicitly.

Before mutation, present or persist the proposed export plan and all unresolved
generic choices. Once `binding.toml` exists, regeneration follows it instead of
making fresh guesses.

## rust-lapper projection

The concrete projection uses generated value structs and opaque state:

```text
Interval { start: usize, stop: usize, val: u32 }
DepthInterval { start: usize, stop: usize, depth: usize }
UnionIntersect { union: usize, intersect: usize }
Lapper: opaque Arc-backed state
Cursor: reusable offset state
iterator handles: opaque, move-only, closeable
```

Never pointer-cast `rust_lapper::Interval<usize, u32>` into the FFI value
struct. Convert fields explicitly in both directions. Opaque Rust objects cross
only as managed handles.

The target ergonomic Mojo API is approximately:

```text
Interval(start, stop, val)
Interval.intersect(other)
Interval.overlap(start, stop)

Lapper(intervals)
Lapper.insert(interval)
Lapper.len()
Lapper.is_empty()
Lapper.cov()
Lapper.set_cov()
Lapper.intervals()
Lapper.merge_overlaps()
Lapper.intersect(other)
Lapper.union(other)
Lapper.union_and_intersect(other)
Lapper.depth()
Lapper.count(start, stop)
Lapper.find(start, stop)
Lapper.seek(start, stop, cursor)

Cursor()
Cursor.reset()
Cursor.value
```

Important semantic details:

- intervals are half-open `[start, stop)`;
- upstream `union` and `intersect` return coverage scalars, not new Lappers;
- upstream `Interval` ordering/equality semantics do not simply include every
  field and must be documented rather than guessed;
- `merge_overlaps` value behavior must match and be tested against upstream;
- adapt empty `depth()` to an empty iterator instead of preserving an upstream
  panic, and record this semantic safety adaptation;
- expose public `intervals` and `overlaps_merged` behavior through safe methods
  or record their projection explicitly;
- evaluate `lower_bound` for a concrete export;
- explicitly defer `bsearch_seq` and `bsearch_seq_ref` in v1.

## ABI and ownership policy

- Map Rust `usize` to C `size_t` and Mojo `UInt`; map `u32` to `UInt32`.
- Use C-compatible primitive structs only after validating size, alignment, and
  field offsets on Rust and Mojo sides.
- Prefer pointers and out-parameters to large aggregate-by-value dynamic calls.
- Declare every Mojo dynamic function with `abi("C")`.
- Load a library once per owning wrapper or function-table lifetime, not once
  per method call.
- Do not cache function pointers beyond the lifetime of the `OwnedDLHandle`
  they borrow.
- Owning Mojo wrappers are move-only and use current Mojo `__deinit__` syntax.
- Upstream `Lapper: Clone` is exposed only as an explicit `clone()` /
  `deep_clone()` operation that creates an independent Rust owner; it never
  makes `Lapper` implicitly copyable.
- Destruction must tolerate null and moved-from handles.
- Iterator `close()` and destruction are idempotent.

Keep the generated layers separate:

```text
_ffi.mojo       exact C ABI declarations/loading/status conversion
_iterator.mojo  move-only Rust iterator ownership and Iterator protocol
lapper.mojo     ergonomic public API
__init__.mojo   public re-exports only
```

## Pixi integration

Preserve existing channels, platforms, dependencies, tasks, and package
metadata. Add only missing Mojo/Rust build requirements and namespaced source
package edges. Never edit `pixi.lock` directly; run Pixi to update it.

The nested generated package uses a deterministic name derived from the root
package, for example:

```text
my-mojo-project-rust-mojo-bindings
```

Its recipe declares Rust/C compilers and pinned Mojo compiler requirements.
Rust is a build dependency, not a run dependency. The packaged output is:

```text
$PREFIX/lib/librust_lapper_mojo_ffi.so       # Linux
$PREFIX/lib/librust_lapper_mojo_ffi.dylib    # macOS
$PREFIX/lib/mojo/rust_lapper.mojoc
```

Development helper tasks may exist, but a task must not be required for a
clean `pixi install` or for artifact publication.

## Skill workflow

The skill executes the operation instead of returning suggestions:

1. Validate that the current directory is a supported Pixi Mojo project.
2. Parse a crates.io version, Git revision, or local-path crate request.
3. Resolve it with Cargo metadata.
4. Inspect public source, docs, examples, and tests.
5. Produce a complete export plan.
6. Reuse explicit generic choices or ask the user for every unresolved choice.
7. Write or update `binding.toml`.
8. Generate the companion Diplomat bridge crate.
9. Run the Diplomat HIR Mojo backend and generate ergonomic wrappers.
10. Generate/update the nested Pixi source package.
11. Patch the root `pixi.toml` idempotently.
12. Let Pixi update its lockfile and build graph.
13. Build and test the Rust bridge.
14. Build/install the shared library and precompile the Mojo package.
15. Compile and run the Mojo integration test without `-I`.
16. Run an installed binary directly outside `pixi run`.
17. Repair generation errors until green.
18. Regenerate a second time and assert a clean diff.
19. Report direct, adapted, monomorphized, opaque, skipped, and
    performance-changing APIs.

## Generated-code policy

Every generated file begins with a deterministic comment appropriate to its
language:

```text
GENERATED FILE - DO NOT EDIT DIRECTLY
Source crate: rust-lapper 1.3.0
Binding manifest: ../binding.toml
Generator version: ...
Diplomat version: ...
Mojo compiler version: ...
```

ABI symbols use deterministic, collision-resistant prefixes such as:

```text
rust_mojo__rust_lapper__lapper_new
rust_mojo__rust_lapper__lapper_destroy
rust_mojo__rust_lapper__lapper_count
rust_mojo__rust_lapper__find_iterator_next
```

Changing the requested crate version updates its bridge, tests, and Cargo lock
in place. It does not append duplicate Pixi entries or leave stale generated
files.

## Tests

The Rust-side bridge test localizes Rust-to-C projection failures. The Mojo
integration test localizes C-to-Mojo failures and verifies at least:

- construction, length, and emptiness;
- interval coordinates and values crossing FFI;
- count and coverage;
- find, interval iteration, depth, and seek lazily advancing one item at a
  time;
- reusable seek cursor semantics;
- insert and coverage recomputation;
- merge-overlaps semantics;
- union, intersect, and named union/intersection result;
- mutation rejection while an iterator lease is active;
- mutation after exhaustion or explicit close;
- repeated construction/destruction without leak or double-free;
- caught panic/error propagation;
- Rust/Mojo ABI sizes, alignments, and offsets;
- import from the installed `.mojoc` with no source `-I`;
- direct execution outside `pixi run` with `CONDA_PREFIX` unset.

Test skill behavior in fresh temporary Hat-style projects. Do not use this
repository itself as the generated target fixture.

## Failure behavior

Treat the pre-mutation state and generated diff as a transaction boundary. Do
not alter upstream source. If a build fails, retain useful generated source for
diagnosis and report:

- the failed phase and concrete compiler/runtime error;
- which bridge and Mojo components were generated successfully;
- all files changed;
- whether `pixi.toml` and `pixi.lock` changed;
- an exact next action.

Do not describe a source-only generation as success. Success means the installed
import compiles and the smoke test runs.

## Milestones

### Milestone A: rust-lapper executable specification

> Historical only. Decision 0001 replaces the checked reference implementation
> below with a fresh-source, general-pipeline acceptance run.

- companion Rust bridge;
- hand-authored/reference Mojo raw and ergonomic wrappers;
- nested Pixi package recipe;
- Rust and Mojo tests passing.

### Milestone B: deterministic generation

- generate the bridge from `binding.toml`;
- generate Mojo raw bindings through Diplomat HIR;
- generate ergonomic wrappers and Pixi integration;
- prove second-run idempotency.

### Milestone C: Codex inspection workflow

- the installable skill resolves and inspects rust-lapper;
- it creates the same manifest and working outputs from a fresh Hat project;
- it pauses for unresolved generic choices.

### Milestone D: demonstrated generalization

- bind a second unrelated crate;
- refine the HIR backend and projection policies from real differences;
- only then claim general Rust-crate support or propose Diplomat upstreaming.

## Non-goals for v1

- arbitrary Rust trait objects;
- async Rust;
- arbitrary callback lifetimes;
- unspecialized generics crossing the ABI;
- zero-copy borrowing iterators;
- Rust native ABI interoperability;
- Windows-native Mojo;
- cross-compilation claims;
- automatic Mojo-to-Rust bindings;
- a single self-contained executable file.

## Acceptance criteria for rust-lapper

The vertical slice is complete only when all of the following are true:

- a fresh Hat-style Pixi Mojo project can invoke the installed skill;
- rust-lapper 1.3.0 resolves without upstream modification;
- generated Rust FFI and Mojo sources are vendored;
- every public API item is classified with reasons for deferrals;
- explicit `usize`/`u32` specialization is recorded;
- the root Pixi graph and lockfile are updated through Pixi;
- `pixi install` builds the source dependency;
- the Rust cdylib and Mojo `.mojoc` are installed under the environment prefix;
- `from rust_lapper import Interval, Lapper` compiles without `-I`;
- construction, count, find, seek, depth, mutation, coverage,
  union/intersection, and destruction pass on both sides of the ABI;
- iterators are lazy and safely exclude mutation while active;
- the installed binary runs directly without `pixi run`;
- native Linux x86-64 passes locally and native Apple silicon passes on the
  user's Mac before those platforms are marked supported;
- a clean checkout can run `pixi install` and then the integration test;
- a second skill invocation is byte-for-byte idempotent.

## Architectural principle

The value of the skill is not that Diplomat magically exports arbitrary Rust.
The value is the complete, tested projection pipeline:

```text
Rust's rich public API
        |
        v
agent-selected semantic FFI projection
        |
        v
Diplomat's constrained HIR and C ABI
        |
        v
generated idiomatic Mojo API
        |
        v
Pixi-built, relocatable package graph
```

For the user, success remains deliberately simple:

```text
"Bind this Rust crate."

-> generated source appears in vendor/
-> Pixi is updated
-> pixi install builds everything
-> from crate_name import ... works
```
