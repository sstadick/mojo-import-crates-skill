# Decision 0002: Follow the target Pixi artifact command

- Status: accepted
- Date: 2026-08-21
- Supersedes: fixed artifact-command claims in `original_plan.md`

## Context

Pixi's preview build interface has changed across releases. The Pixi available
in the development environment exposes `pixi build --output-dir`; newer local
documentation describes `pixi publish --target-dir` as the package-artifact
operation. A Hat project can also require a newer Pixi than the executable
currently first on `PATH`.

The binding package graph is the stable product requirement. A hard-coded CLI
spelling is not.

## Decision

The skill first selects a Pixi executable compatible with the target project's
`requires-pixi`, then inspects its `build`, `publish`, and `install` help. It
passes that decision to the deterministic integrator as
`--artifact-mode build|publish`. The modes deliberately produce different
package manifests as well as different reported commands:

- `build` omits `[package].publish` from every generated manifest. It reports
  two ordered commands: first
  `pixi build --manifest-path vendor/rust-bindings/pixi.toml --output-dir ...`,
  then `pixi build --output-dir ...`. This is the Pixi 0.59-compatible form.
  The first command exports the aggregate Rust/Mojo dependency artifact; the
  second exports the application artifact. Building only the root may build a
  source dependency internally without exporting the dependency artifact,
  which is not a closed set for installation elsewhere.
- `publish` adds `publish = true` to generated source packages and reports
  `pixi publish --target-dir ...`. This is the Pixi 0.76+ multi-package form.

If the skill does not pass an explicit mode, a target `requires-pixi` lower
bound of 0.76 or newer selects `publish`. Missing, older, disjunctive, or
otherwise ambiguous constraints conservatively select `build`. Thus the
integrator is deterministic even when invoked directly, while an inspected
Pixi executable remains the authoritative choice in the skill workflow.

The integrator owns only `publish` keys that it inserted. It records that
ownership in aggregate provenance, removes its own key when changing back to
`build`, and never overwrites or removes a user-authored setting. A user-owned
`publish` key retained in `build` mode is reported as a compatibility warning.
Publish mode cannot form a complete workspace artifact set when the root
package explicitly opts out with user-owned `publish = false`. The integrator
therefore fails before mutating any files in that case, instead of adding
`publish = true` only to the generated nested package or reporting an unusable
publish command. The error directs the user to enable root publication or
select `--artifact-mode build`.

The skill must validate `pixi install` for clean-checkout development and
invoke the selected local-artifact operation(s). Its final external-install
proof consumes the complete artifact set, not only the root package.

The final report names the exact command and artifacts tested. Generated source
package correctness cannot depend on Pixi tasks.

## Consequences

- The requested smooth `pixi build` path works on Pixi releases that provide
  it.
- The skill does not reject a newer project merely because Pixi renamed or
  moved the artifact operation.
- Changing artifact modes is idempotent and works with `--check`; generated
  publication metadata does not become sticky.
- Build mode produces two distributable artifacts in dependency-first order;
  publish mode delegates that multi-package ordering to Pixi's publication
  workflow.
- A root package that explicitly sets `publish = false` must use build mode or
  opt into publication; publish mode never leaves behind a partial mutation.
- Tests must distinguish the Pixi version actually exercised from other
  supported manifest shapes.
