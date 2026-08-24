# Binding manifest schema v1

Read this reference when creating or regenerating a binding. `binding.toml` is
the human-reviewed audit record for source identity and user-visible decisions;
`ffi/src/lib.rs` is the reviewed executable semantic projection.
`manifest.json` is generated provenance and must not replace either input.

Create the complete binding in a temporary staging directory. On an unchanged
regeneration, preserve that binding's own manifest and audited bridge
byte-for-byte after validating them against the exact resolved source. Then
regenerate the ABI report and Mojo package mechanically. Never copy a bridge or
wrapper from a different binding. Re-inspect and explicitly revise the bridge
when the source, features, scope, or recorded decisions change. See
[Decision 0004](../plans/decisions/0004-semantic-projection-source.md).

## Required integration fields

The generic integrator validates these fields:

```toml
schema_version = 1

[binding]
id = "example_binding"
mojo_package = "example_binding"

[crate]
name = "example-crate"
version = "=1.2.3"
source_kind = "registry"
features = []
default_features = true
checksum = "<64-lowercase-hex crates.io checksum>"

[ffi]
crate_name = "example_binding_ffi"
cargo_manifest = "ffi/Cargo.toml"
tests = ["ffi/tests/bridge.rs"]

[mojo]
source_dir = "mojo/example_binding"
tests = ["tests/smoke.mojo"]

[pixi]
build_dependencies = []
host_dependencies = []
run_dependencies = []
```

`binding.id` is the vendored directory name. `crate.version` is always an exact
version beginning with `=`. Registry sources require a 64-character lowercase
checksum. Git sources use the same required feature fields but replace
`checksum` with `git` and a full lowercase 40-character `rev`:

```toml
[crate]
name = "example-crate"
version = "=1.2.3"
source_kind = "git"
features = ["feature-a"]
default_features = false
git = "https://github.com/example/example-crate.git"
rev = "0123456789abcdef0123456789abcdef01234567"
```

Local-path resolution is supported for inspection, but packaging is deferred
until the generator can create and validate a self-contained source snapshot.

`ffi.crate_name` is the Cargo `cdylib` target identifier and therefore the stem
of `lib<crate_name>.so` or `lib<crate_name>.dylib`. Paths are relative to the
binding root. The Cargo manifest must have a `Cargo.lock` at its directory or
an ancestor inside the binding root. `ffi.tests` must name at least one direct
Cargo integration test under that crate's `tests/` directory, and each named
file must contain an explicit `#[test]` outside comments and literals. The
generated package recipe also asks Cargo to list that target and fails if it
contains zero actual tests before running it. The Mojo source directory must
contain `__init__.mojo`; `mojo.tests` must contain at least one integration test.
Optional Pixi dependency arrays contain ordinary conda MatchSpec strings
required by this binding; do not repeat `mojo-compiler`, whose exact constraint
is inherited from the root Mojo package.

The binding root must also contain `abi-report.json`. Before copying anything,
the integrator invokes `generate_mojo_package.py --check` to validate the
complete export ledger, report model/body digests, finalized `_ffi.mojo`, exact
wrapper contents, and closed `.mojo` source inventory.

The `[tools]` table shown below is also required by the generic integrator. It
is closed: it contains exactly `generator_version`, `abi_backend_version`,
`diplomat_version`, `diplomat_core_version`, `diplomat_runtime_version`, and
`mojo_version`, with no missing or additional keys. Every value is a bare,
exact semver-like version such as `0.16.1`, not a range or branch name.

The companion manifest must directly depend on the requested upstream package
with the same exact source and feature policy, and its library target must
include `cdylib`. The integrator cross-checks the declaration, `Cargo.lock`,
resolver output, and `binding.toml`; renamed Cargo dependency keys are fine when
their `package` identity matches. Every path dependency in the companion graph
must stay inside the staged binding.

The companion's root `[dependencies]` must also declare non-optional, exact
registry dependencies on `diplomat = "=<tools.diplomat_version>"` and
`diplomat-runtime = "=<tools.diplomat_runtime_version>"`. Its `Cargo.lock` root
package must resolve unambiguously to both selected registry packages, and the
selected `diplomat` package must resolve to the recorded `diplomat_core`
version. Unrelated versions elsewhere in the graph are allowed. The integrator
rejects any missing, ambiguous, non-registry, or mismatched edge before copying
files. It likewise normalizes the root project's exact `=` or `==` Mojo
compiler requirement and requires it to equal
`tools.mojo_version` before project mutation.

The Mojo 1.0 dynamic-call boundary is scalar/pointer-only. A semantic bridge
must project borrowed slices to private `usize` address and length parameters,
take value structs through immutable/mutable struct pointers, and place
aggregate results in caller-owned mutable value structs. Do not map a raw ABI
function that takes or returns a slice carrier or value struct by value merely
because the generated Mojo declaration compiles. Represent an owned byte or
value collection as an opaque owner with scalar/pointer accessors and an opaque
destructor; the raw `DiplomatOwnedSlice` carrier is also an aggregate.

## Semantic decisions and export audit

Record enough information to audit the projection without guessing.
`generate_mojo_package.py` validates the export ledger and the closed
ABI-to-Mojo map, and the integrator requires that validation to pass again
before packaging.

```toml
[binding]
symbol_prefix = "rust_mojo__example_binding__"

[scope]
requested = ["Buffer", "Buffer::len", "Buffer::iter"]
trait_policy = "Only requested trait behavior is in scope."
audited_exports = [
  "buffer.type",
  "buffer.len",
  "buffer.entry",
  "buffer.iter",
  "buffer.deferred-trait",
]

[tools]
generator_version = "0.1.0"
abi_backend_version = "0.1.0"
diplomat_version = "0.16.1"
diplomat_core_version = "0.16.1"
diplomat_runtime_version = "0.16.0"
mojo_version = "1.0.0"

[[specializations]]
rust_type = "example_crate::Buffer<T, N>"
mojo_name = "Buffer"
parameters = { T = "u32", N = "8" }
exports = ["buffer.type", "buffer.len", "buffer.iter"]
chosen_by = "user"

[[specializations]]
rust_type = "example_crate::Entry<T>"
mojo_name = "Entry"
parameters = { T = "u32" }
exports = ["buffer.entry", "buffer.iter"]
chosen_by = "user"

[[adaptations]]
export = "buffer.iter"
rust = "example_crate::Buffer<u32, 8>::iter"
mojo = "Buffer.iter"
kind = "lazy-rust-iterator"
effect = "Each next call crosses the C ABI; no collection is materialized."
chosen_by = "user"

[[exports]]
id = "buffer.type"
rust = "example_crate::Buffer<u32, 8>"
mojo = "Buffer"
status = "OPAQUE"
reason = "The Rust layout stays behind a uniquely owned handle."

[[exports]]
id = "buffer.len"
rust = "example_crate::Buffer<u32, 8>::len"
mojo = "Buffer.len"
status = "MONOMORPHIZED"
reason = "T=u32 and N=8 were selected by the user."

[[exports]]
id = "buffer.entry"
rust = "example_crate::Entry<u32>"
mojo = "Entry"
status = "MONOMORPHIZED"
reason = "Iterator items use the user-selected u32 element type."

[[exports]]
id = "buffer.iter"
rust = "example_crate::Buffer<u32, 8>::iter"
mojo = "Buffer.iter"
status = "ADAPTED"
reason = "A retained Rust iterator performs one next call per Mojo step."

[[exports]]
id = "buffer.deferred-trait"
rust = "example_crate::Buffer<u32, 8>::some_unrequested_trait_method"
status = "SKIPPED"
reason = "The method is outside the agreed scope."
```

These six keys are the complete `[tools]` schema. They are independently bound
to generated evidence: ABI report schema 3 authenticates the backend and
`diplomat_core` versions and hashes both the canonical ABI model and raw Mojo
body, the companion Cargo
manifest and lock bind the Diplomat crates, and the target Pixi manifest binds
the Mojo compiler. Do not update a tool claim without regenerating and
revalidating its corresponding evidence.

Every export has a stable, binding-local `id`. Use exactly one status:
`DIRECT`, `ADAPTED`, `MONOMORPHIZED`, `OPAQUE`, or `SKIPPED`. A non-skipped
export requires a non-empty `mojo` name and at least one public mapping linked
with `export = "<id>"`. A skipped export has no `mojo` field and no public
mapping. Multiple ABI types/functions may link to one export; this is necessary
for projections such as an iterator factory, iterator owner, and `next` method
that jointly implement one Rust API.

`scope.audited_exports` is a required, non-empty, duplicate-free inventory whose
set must exactly equal every `[[exports]].id`, including skipped records. This
closes the manifest's own semantic ledger: an export cannot be added or removed
without updating the declared audit inventory. It does **not** mechanically
prove that the audit discovered every relevant API in the upstream Rust source;
source inspection and the recorded scope remain responsible for that semantic
completeness.

Each `[[adaptations]]` record names one `export`. That export must have status
`ADAPTED`, and the decision's `rust` and `mojo` strings must exactly equal the
linked export's names. Every `ADAPTED` export has exactly one adaptation record.
Each `[[specializations]]` record likewise has a non-empty, duplicate-free
`exports` array naming non-skipped exports affected by that concrete choice.
Every `MONOMORPHIZED` export is linked by at least one specialization. These are
explicit semantic links; do not infer them from substrings in Rust type names.

Every ordinary public mapping must render exactly the `mojo` name of its linked
export: a public type renders its `mojo_name`, a free function renders its
`mojo_name`, a method/static/named constructor renders the owner's public Mojo
type plus `.` and its `mojo_name`, and a constructor renders its owner's public
type. Give otherwise independent constructors, methods, and helpers separate
export records rather than hiding them under a broad type or method export. The
only mismatching support mappings allowed are an iterator owner type and its
`iterator_next` callable linked to the same `ADAPTED` factory export; another
linked mapping must still render that factory export's declared public surface.

Every exposed specialization and every material adaptation requires
`chosen_by = "user"`. An unchanged regeneration preserves that original
reviewed record byte-for-byte, so reuse does not replace it with a new
`chosen_by` value. The wrapper generator rejects an agent-selected concrete
generic or material adaptation. Examples and agent inference are evidence, not
authorization.

Use the status describing the strongest user-visible projection in this order:
`SKIPPED`, `ADAPTED`, `OPAQUE`, `MONOMORPHIZED`, `DIRECT`. Record other facts in
`[[specializations]]` and `[[adaptations]]`. Every API item in the agreed scope
gets an export record and every skip gets a concrete reason.

## Closed Mojo type map

The ABI report schema is version 3. It records the exact backend/core versions,
a canonical SHA-256 digest over all report declarations, and a separate SHA-256
digest of the complete raw Mojo ABI body. Wrapper generation rejects a changed
report layout or carrier as well as a changed raw declaration or extra symbol,
even if individual function aliases still look valid. The raw backend header
embeds the same model digest, so recomputing a digest in an edited report cannot
make it match an unchanged `_ffi.mojo`. Add exactly one
`[[mojo.types]]` entry for every struct, fieldless enum, and opaque type in that
report. Nothing is implicitly public and nothing may silently disappear.

```toml
[[mojo.types]]
abi_name = "Entry"
mojo_name = "Entry"
kind = "value"
export = "buffer.entry"

[[mojo.types]]
abi_name = "Buffer"
mojo_name = "Buffer"
kind = "opaque"
destroy_abi_symbol = "rust_mojo__example_binding__buffer_destroy"
export = "buffer.type"

[[mojo.types]]
abi_name = "OperationStatus"
kind = "skip"
reason = "Internal status enum translated into Mojo errors."
```

`kind` is one of `value`, `enum`, `opaque`, `iterator`, or `skip`. Public kinds
require `mojo_name` and `export`; `skip` requires `reason` and must not link an
export. An opaque `destroy_abi_symbol` must exactly match the ABI report. Public
value structs may recursively contain primitives, exported values, and
exported fieldless enums only.

An out struct used only as private caller-owned storage remains `kind = "skip"`.
The function's `result.value_fields` policy may still project fields from that
raw carrier into a public scalar or synthetic result. Do not give an internal
carrier an underscore-prefixed public `mojo_name`; every non-skipped value type
is deliberately re-exported from `__init__.mojo`.

A lazy Rust-backed iterator is an opaque type with a scalar-status/mutable-out
step protocol:

```toml
[[mojo.types]]
abi_name = "EntryIterator"
mojo_name = "EntryIterator"
kind = "iterator"
destroy_abi_symbol = "rust_mojo__example_binding__entry_iterator_destroy"
next_style = "status-out"
out_param = "out"
item_discriminant = 0
finished_discriminant = 1
error_discriminants = { "2" = "The Rust iterator panicked." }
export = "buffer.iter"
```

The corresponding `iterator_next` function must take only mutable `self` and a
mutable pointer to an exported value struct, and return an integer or fieldless
enum status. The generated explicit `next()` raises a Mojo `Error` for declared
error statuses. Mojo 1.0's `Iterator.__next__` may raise only `StopIteration`, so
it aborts on an FFI error instead of reading an unwritten output or silently
truncating iteration.

## Closed Mojo function map

Add exactly one `[[mojo.functions]]` entry for every ABI-report function. The
generator cross-checks `abi_owner`, `rust_name`, and `abi_symbol` against the
report. Every bridge symbol and opaque destructor must start with
`binding.symbol_prefix`.

```toml
[[mojo.functions]]
abi_owner = "Buffer"
rust_name = "len"
abi_symbol = "rust_mojo__example_binding__buffer_len"
mojo_name = "len"
kind = "method"
out_param = "out"
result = { ok_discriminant = 0, errors = { "1" = "Rust panic" }, value_fields = ["value"] }
export = "buffer.len"

[[mojo.functions]]
abi_owner = "EntryIterator"
rust_name = "next"
abi_symbol = "rust_mojo__example_binding__entry_iterator_next"
mojo_name = "__next__"
kind = "iterator_next"
export = "buffer.iter"

[[mojo.functions]]
abi_owner = "Buffer"
rust_name = "abi_layout_helper"
abi_symbol = "rust_mojo__example_binding__abi_layout_helper"
kind = "skip"
reason = "Internal layout assertion, not a user-facing API."
```

Function `kind` is `constructor`, `named_constructor`, `static`, `method`,
`free`, `iterator_next`, or `skip`. A public function requires `mojo_name` and
`export`; a skipped raw helper requires `reason` and no export link. A
constructor's public name is `__init__` and it returns its owned opaque owner.
Use `optional_none_error` on an optional owned-opaque return when `None` should
raise rather than become a public `Optional`.

### Scalarized borrowed inputs

Mojo 1.0 dynamic FFI must not receive Diplomat's slice carrier by value. Make
the bridge accept private `usize` address and length parameters, then collapse
them to one public argument:

```toml
[[mojo.functions]]
abi_owner = "Buffer"
rust_name = "new"
abi_symbol = "rust_mojo__example_binding__buffer_new"
mojo_name = "__init__"
kind = "constructor"
scalarized_params = [
  { name = "entries", kind = "copied-value-slice", element = "Entry", data_param = "entries_data", len_param = "entries_len" },
]
optional_none_error = "Buffer construction failed"
export = "buffer.type"
```

Each raw data/length parameter must be `usize` (`u_int` in the ABI report).
Supported scalarized kinds are:

- `copied-value-slice`: copy public values to their ABI-safe value structs for
  the duration of the call; `element` is an ABI type name.
- `borrowed-primitive-slice`: borrow an immutable Mojo span for the call;
  `element` is an ABI primitive spelling such as `u_int8`.
- `utf8-string`: borrow `String.as_bytes()` for the call and require
  `element = "u_int8"`.

The Rust bridge must treat address zero as valid only for an empty input and
validate alignment, checked byte length, `isize::MAX`, and address overflow
before creating a slice. This is a private generated call shape, never a public
raw-address Mojo API. Because converting a Mojo pointer to an integer severs
origin tracking, generated code must make an explicit post-call use of every
String/span/list that backs such an address. Apply the same keepalive rule to
local ABI values passed through untracked pointers.

### Scalar status and aggregate out values

For an aggregate result, add a final mutable value-struct pointer to the bridge,
return a scalar status, and name that parameter with `out_param`. The `result`
policy covers every status discriminant and lists fields to read only after the
success status. One field becomes the return value; multiple fields require a
synthetic `mojo_type` and may be renamed with `value_names`:

```toml
out_param = "out"
result = { ok_discriminant = 0, errors = { "1" = "Rust panic" }, value_fields = ["union_value", "intersect_value"], mojo_type = "UnionIntersect", value_names = { union_value = "union", intersect_value = "intersect" } }
```

When the out struct itself is an exported public value, return the whole value
instead of projecting fields:

```toml
out_param = "out"
result = { ok_discriminant = 0, errors = { "1" = "Rust panic" }, out_value = true }
```

`out_value = true` cannot be combined with `value_fields`, `mojo_type`, or
`value_names`. The generator zero-initializes the out struct, keeps its storage
alive through the call, checks the scalar status, and converts the value only
on the success path.

For a scalar-status operation with no returned value, omit `out_param` and set
`value_fields = []`. Do not return a status/result carrier struct by value.

Record every ABI function symbol explicitly in `[[mojo.functions]]` and every
opaque destructor in `[[mojo.types]]`. Derive a stable prefix from `binding.id`
by replacing `-` with `_`. Names must be unique and remain unchanged once written.
When otherwise identical readable names collide, append the first 12 lowercase
hex characters of SHA-256 over the fully qualified Rust item plus its concrete
Rust signature. Verify the bridge's `abi_rename` values against the backend JSON
report.

For Git sources, record the URL and full 40-character commit. For local sources,
record the project-relative source path and fingerprint. A local dependency must
also be made self-contained in the packaged binding source graph; if that cannot
be done without copying or restructuring user source, stop and explain that
packaging is not yet supported for that source rather than emitting a binding
that only works from the current checkout.

Do not store absolute cache paths, usernames, environment prefixes, Cargo's
temporary `manifest_path`, timestamps, or other host-specific values.

## Staging tree

The staging directory supplied to `integrate_binding.py` should contain:

```text
binding.toml
abi-report.json
ffi/
  Cargo.toml
  Cargo.lock
  src/lib.rs
  tests/...
mojo/<mojo_package>/
  __init__.mojo
  _ffi.mojo
  ...
tests/...
```

Every generated source file starts with a deterministic header naming the
resolved source crate/version, `binding.toml`, generator and Diplomat versions,
and the Mojo compiler version. Do not put a timestamp in the header.
