# Decision 0004: Keep the semantic bridge as audited source

- Status: accepted
- Date: 2026-08-21

## Context

`binding.toml` can record source identity, requested scope, user-selected
specializations, adaptations, export classifications, public wrapper roles, and
ABI symbols. It cannot reproduce an arbitrary adaptation body such as lifetime
management, panic recovery, an error projection, or a crate-specific invariant
without embedding a new Rust-like programming language in TOML.

Requiring an agent to resynthesize those bodies on every unchanged run would
also undermine idempotency. A semantically equivalent bridge could differ in
source spelling, and a superficially similar bridge could accidentally change
ownership or error behavior.

## Decision

The binding has two reviewed semantic inputs:

1. `binding.toml` is the source of truth for source identity, user decisions,
   API scope, export audit, ABI names, and Mojo wrapper roles.
2. `ffi/src/lib.rs` is the executable semantic projection. It contains the
   crate-specific Rust code that implements those recorded decisions.

The Diplomat ABI report and all Mojo sources are deterministic mechanical
outputs of those inputs. The wrapper generator must reject missing, duplicate,
or mismatched manifest-to-ABI mappings rather than silently omitting them.

For a new binding, the agent always derives a fresh bridge from the exact
resolved upstream source. It may not copy or parameter-substitute another
crate's bridge. For an unchanged crate identity, features, requested scope, and
manifest decisions, regeneration validates the existing bridge against the
resolved Cargo graph, manifest symbols, Diplomat HIR report, and tests, then
preserves the bridge byte-for-byte. For a version, feature, scope, generic, or
adaptation change, the agent re-inspects the upstream API and revises the bridge
explicitly before regenerating mechanical outputs.

The generated provenance inventory hashes both semantic inputs and every
mechanical output. An unchanged run must produce no source, manifest, Pixi, or
lockfile diff.

## Consequences

- The project does not need a second Rust parser or an unsafe mini-language for
  arbitrary adaptation bodies.
- Idempotency does not depend on repeated generative sampling producing
  identical Rust spelling.
- `binding.toml` remains sufficient to audit every user-visible decision, but
  is not falsely described as a complete executable implementation.
- A version or configuration update has an explicit semantic review boundary.
- Crate-specific bridge source is valid inside its own generated binding; it is
  never a production template for another binding.
