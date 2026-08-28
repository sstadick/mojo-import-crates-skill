# Decision 0006: Process-global runtime state with once-resolved pointers

- Status: accepted
- Date: 2026-08-24

## Context

The first wrapper scheme gave every opaque object its own `OwnedDLHandle`
(a `dlopen` per object) and resolved the symbol with `get_function` inside
every method call (a `dlsym` plus allocation per call), because the skill
believed Mojo 1.0 had no supported runtime-global storage. Measured on a real
binding with optimized builds, a trivial scalar getter cost ~290 ns per call
while the raw resolved C call costs ~1 ns — the FFI crossing itself is nearly
free; the wrapper plumbing dominated.

The Mojo standard library already solves this for CPython: `std.ffi._Global`
stores a process-wide, init-once state struct via
`KGEN_CompilerRT_GetOrCreateGlobal`, and the `CPython` struct resolves every
interpreter function pointer exactly once into typed fields at init.

An intermediate design that cached pointers in *named* compiler-runtime
globals per symbol was rejected: building the `String` cache key per call
cost as much as the `dlsym` it replaced.

## Decision

`_runtime.mojo` follows the CPython pattern:

1. A `_RuntimeState` struct holds `Optional[OwnedDLHandle]`, a `load_error`
   `String`, and one typed field per ABI symbol (functions and opaque
   destructors), each typed by its `_ffi` comptime alias.
2. `_RuntimeState.__init__` opens the library once; on success it resolves
   every field with `handle._get_function[symbol, alias]()`, on failure it
   fills fields with null pointers and records the error.
3. `comptime _LIBRARY_GLOBAL = _Global[StorageType=_RuntimeState, ...]`
   stores it process-wide; `_functions() raises` returns the state pointer
   and raises `load_error` if the library never loaded, preserving the
   raising semantics of the old per-object open.
4. Wrappers read `_runtime._functions()[].<alias>` and call it. Opaque
   structs keep only their `_handle`; the `_library` field, per-object
   `dlopen`, and per-call `dlsym` are gone. Destructors use the same fields
   inside the existing try/abort guard.

## Consequences

- Wrapper scalar calls drop from ~290 ns to ~9 ns; bulk fills remain the
  right answer for arrays (21k doubles in a few microseconds).
- The library handle lives for the process, so resolved pointers can never
  outlive it, satisfying the handle-lifetime rule globally.
- `_Global` and `_get_function` are underscore-private stdlib APIs verified
  against Mojo 1.0.0; re-verify with a small probe before trusting them on a
  new compiler, and fall back to per-object handles if they vanish.
- `_Global` vends unsynchronized shared pointers (same caveat as CPython);
  the state is written once at init and read-only afterwards.
