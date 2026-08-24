# diplomat-gen-mojo

An initial, out-of-tree Mojo backend experiment for Diplomat 0.16.1. It lowers
self-contained inline Rust bridge modules through `diplomat_core` HIR and emits
a deterministic Mojo ABI schema and function-signature layer. It deliberately
does not parse Rust a second time.

```sh
cargo run --manifest-path crates/diplomat-gen-mojo/Cargo.toml -- \
  bridge.rs -o _ffi.mojo --report abi.json --deny-unsupported
```

The current supported surface models primitive value structs, immutable/mutable
references to those structs, opaque pointers and destructors,
constructors/methods/free functions, fieldless 32-bit C enums, nullable opaque
pointers, and primitive/struct slices. Unsupported HIR constructs are retained
in the generated file as an explicit report and are also returned by the
library API after HIR construction succeeds. Parse errors, external bridge
modules, and HIR-construction failures stop before a report can be produced.
`--deny-unsupported` makes the CLI fail after writing a partial-lowering report.

With Diplomat 0.16.1, put a default namespaced
`#[diplomat::abi_rename = "prefix__{0}"]` on the bridge module. The backend
rejects function-level `abi_rename` on a free function before HIR lowering
because that version's proc macro accepts the syntax but can fail to consume
the attribute. Method-level overrides remain supported.

ABI report schema 3 does not encode host-language lifetime edges. The backend
therefore rejects any return value that Diplomat reports as borrowing from a
method input. A semantic bridge may instead return a genuinely self-owned
opaque handle—for example, a lazy iterator that retains its owner and borrow
guard internally—so no Rust borrow depends on a separate Mojo wrapper.
Enum receiver methods are also rejected for now: Diplomat 0.16.1 lowers their
`self` parameter as a pointer, while schema 3 intentionally has no enum-pointer
type. A bridge can expose the operation as a free function until that shape is
modeled exactly.

`--report` emits deterministic JSON with a numeric schema version, all lowered
structs, opaque types, enums, functions, and referenced ABI types, plus every
unsupported item and its reason. Function and opaque records include the exact
Mojo `thin abi("C")` aliases emitted by the raw layer so wrappers and tests can
verify the declared C signature. It also records backend provenance, a canonical
SHA-256 digest of the report's complete ABI model, and a separate SHA-256 digest
of the complete raw Mojo body. A later stage can therefore reject edits to either
side of that contract, including report-only layout or carrier changes and raw
unreported declarations. The backend also embeds the model digest in the raw
Mojo header, binding the report and raw ABI as one generated pair. Loader APIs
differ by Mojo version: Mojo 1.0's
`OwnedDLHandle.get_function` takes only a return-type parameter and infers its
arguments at the call site. Ergonomic wrappers for that compiler must therefore
keep the dynamic boundary scalar/pointer-only; passing a C aggregate by value
can compile and still be mislowered. The JSON report is written even when
`--deny-unsupported` turns a partial lowering into a non-zero exit.

The raw declarations use C-ABI-compatible trivial Mojo carriers. Immutable
borrowed slices retain an immutable pointer origin, while mutable and owned
slices use mutable pointer origins. Owned byte-slice returns model Diplomat's
`diplomat_owned_slice_u8_destroy` symbol. When a function consumes owned bytes,
the raw layer also exposes `diplomat_alloc` and `diplomat_free`: wrappers must
copy into Rust-allocated memory before transferring the carrier, rather than
letting Rust reconstruct a `Box` around Mojo-allocated storage. Other owned
element types are rejected until a companion bridge supplies an
element-specific destroy function; treating their allocation as generically
freeable would be unsound.

This is still a raw ABI backend, not the ergonomic ownership layer. A generated
wrapper must load and retain the shared-library handle, resolve each recorded
symbol, call opaque destructors exactly once, and keep borrowed inputs alive for
the call. The Mojo 1.0 ergonomic generator intentionally rejects the raw
two-word owned-byte carrier at the dynamic-call boundary; project such a value
to an opaque Rust owner with scalar/pointer accessors instead. Result and scalar
Option carriers, DiplomatWrite string returns, callbacks, opaque slices,
traits, and 128-bit integers remain explicitly unsupported. Result support in
particular requires an exact union carrier; string-writing returns add a
`DiplomatWrite*` ABI parameter and cannot be modeled as an ordinary direct
return.
