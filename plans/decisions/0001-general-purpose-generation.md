# Decision 0001: Generate every production binding from its source

- Status: accepted
- Date: 2026-08-21
- Supersedes: the checked-template allowance in `original_plan.md`

## Context

The original vertical-slice plan allowed a hand-authored rust-lapper bridge and
Mojo package to be checked in and copied into target projects while the
generator matured. That is useful for proving one ABI, but it changes the
product: the apparent “general” skill becomes a rust-lapper installer with
crate-specific branches. Passing that path does not test whether source
inspection, manifest creation, HIR lowering, wrapper generation, or generic
integration works for a new crate.

The intended product is an agent-driven generator for arbitrary requested Rust
crates. A pinned crate can be an acceptance case without becoming a production
input.

## Decision

Every production invocation starts from the exact resolved Rust source and a
binding manifest created for that request. The agent creates a new companion
bridge, runs the general Diplomat-to-Mojo backend, creates ergonomic wrappers,
and invokes a crate-agnostic Pixi integrator.

This fresh-generation rule applies across binding identities and whenever the
source or semantic configuration changes. An unchanged binding preserves and
revalidates its own audited semantic bridge as specified by Decision 0004; that
bridge still may not be reused as a template for another crate.

Checked crate-specific bridges, wrappers, manifests, recipes, and generated
tests must never be copied, renamed, token-substituted, or selected by a
production code path. If retained at all, such files are fixtures for isolated
backend tests and cannot be an installer input.

Acceptance cases contain only declarative requests and observable expectations.
The rust-lapper case pins source identity and desired semantics, but generation
must begin from its resolved crates.io source exactly as it would for any other
crate.

## Consequences

- A rust-lapper-only copier is not part of the product and cannot establish an
  end-to-end success claim.
- The generic backend and integrator may initially fail on new HIR/API shapes;
  those failures must be reported or repaired rather than bypassed with a
  fixture.
- Acceptance evaluation becomes a meaningful test of the full pipeline.
- The original plan remains available as historical context, but this decision
  controls wherever its template allowance conflicts.
