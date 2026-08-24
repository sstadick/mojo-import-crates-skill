# Decision 0003: Start with an out-of-tree Diplomat Mojo backend

- Status: accepted
- Date: 2026-08-21

## Context

There are three plausible implementation boundaries:

1. add Mojo directly to the upstream Diplomat repository;
2. generate Mojo independently between a hand-written C bridge and the user
   package; or
3. implement a real Diplomat backend out of tree, using `diplomat_core` HIR.

An upstream backend gives the best eventual discoverability and reduces
long-term drift, but it couples the first experiments to upstream review and
release cadence before the required Mojo ABI and ownership shapes are known.
An independent middle layer is fast to prototype, but it would duplicate
Diplomat's type analysis and could silently disagree with the C ABI.

Diplomat's own [backend developer guide](https://rust-diplomat.github.io/diplomat/developer.html)
supports third-party backends built on `diplomat_core`, so the out-of-tree
choice still uses Diplomat's intended extension boundary.

Neither alternative solves the semantic projection from an arbitrary Rust API:
the agent must still decide specializations, ownership, error models, iterator
state, and the API exposed to Mojo.

## Decision

Implement `diplomat-gen-mojo` as an out-of-tree backend over the pinned
`diplomat_core` HIR. The agent generates an explicit companion Diplomat bridge;
the backend owns deterministic raw ABI lowering and a machine-readable report;
the agent generates ergonomic Mojo ownership and API wrappers from the manifest
and report.

Do not add a second Rust parser. Do not patch a user's upstream crate. Consider
contributing the backend upstream only after at least two unrelated real crates
exercise its design and the Mojo ABI surface has stabilized.

## Consequences

- This repository can iterate and release without waiting for Diplomat.
- Raw lowering shares Diplomat's constrained FFI model instead of inventing a
  parallel one.
- The project temporarily owns compatibility with pinned Diplomat releases.
- Ergonomic wrapper generation remains outside Diplomat until repeated patterns
  justify moving more mechanics into the backend.
- Upstreaming later is a migration of a proven backend, not a rewrite of an
  independent binding system.
