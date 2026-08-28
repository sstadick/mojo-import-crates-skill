# Decision 0005: Named private registries and the vendored upstream copy

- Status: accepted
- Date: 2026-08-24

## Context

The first real crate bound outside the acceptance suite is published only to
a private alternate Cargo registry and its Git mirror is private. Resolution and developer builds work on a configured
machine, but Pixi package builds run under rattler-build, which redirects
`HOME` and `CARGO_HOME` into the build work directory. Inside that sandbox no
git credential helper, keychain, cargo token, or user cargo config exists, so
any credentialed fetch of a private index or crate fails with
`could not read Username ... Device not configured`. Public crates.io
dependencies still fetch normally because they need no credentials.

`net.git-fetch-with-cli = true` and OS keychains fix interactive use but can
never fix the hermetic build, because the sandbox deliberately hides the
user identity that those mechanisms depend on.

## Decision

1. The resolver accepts `--registry-name NAME --registry-index URL` alongside
   `--registry NAME@VERSION`. The probe writes a temporary
   `.cargo/config.toml` defining the registry (plus `git-fetch-with-cli`) and
   runs Cargo from the probe directory so config discovery applies.
2. `binding.toml [crate]` gains optional `registry` and `registry_index`
   keys, valid only with `source_kind = "registry"`. The integrator validates
   them, records them in provenance, and requires the locked upstream source
   to match the declared index.
3. The generated recipe passes the registry definition through Cargo's
   environment configuration (`CARGO_REGISTRIES_<NAME>_INDEX`, plus
   `CARGO_NET_GIT_FETCH_WITH_CLI` and the token credential provider) so the
   closed aggregate build context needs no checked-in config files.
4. For private sources the binding ships a checksum-verified copy of exactly
   the upstream crate at `ffi/vendored/<crate-name>/` in `cargo vendor`
   layout (`.cargo-checksum.json` whose `package` hash must equal
   `[crate].checksum`; any `.cargo` directory inside the published crate is
   stripped and omitted from the file map). The recipe's first script steps
   write a build-local `.cargo/config.toml` replacing only that registry
   source with the in-tree directory. The declared dependency, lockfile
   entry, and checksum are unchanged — the copy participates purely through
   source replacement, so provenance still names the registry.

## Consequences

- Hermetic package builds need no credentials for the private crate while
  crates.io dependencies continue to fetch anonymously.
- The repository carries one extracted crate copy per private upstream
  (hundreds of kilobytes, checksum-pinned), not a full dependency vendor.
- Developer-side lock regeneration still uses the live registry and therefore
  still needs machine credentials; only packaged builds are credential-free.
- A vendored copy whose package checksum drifts from `[crate].checksum` is
  rejected before any project mutation.
