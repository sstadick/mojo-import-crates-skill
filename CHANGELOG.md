# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Optional standalone-library mode with a deterministic pixi-build-mojo
  repository scaffold, Git-dependency consumer, cross-platform CI, strict API
  documentation, executable examples, and GitHub Pages-ready site generation.
- A standalone completion contract for API-specific README content, public
  facade tests, examples, generated documentation, and hosted-operation
  boundaries.

### Fixed

- Mojo 1.0 status/out wrappers now reload caller-owned result storage through a
  non-inlined pointer boundary, preventing stale initialized values after
  dynamically loaded Rust calls. Status/out iterators use the same safe reload.
- Aggregate binding artifacts no longer package Mojo compiler caches,
  crash-report state, or first-activation markers created during native tests.
- The standalone docs scaffold now generates a function page and executes an
  API doctest from a normal Mojo submodule, avoiding Modo's omission of
  functions defined in `__init__.mojo`.

## [0.1.0] - 2026-08-28

### Added

- Initial Rust-to-Mojo binding skill, including exact crates.io and immutable
  Git resolution, closed-schema wrapper generation, Pixi integration, and
  regeneration checks.
- Named alternate and private Cargo registry resolution with checksum and
  registry-index validation.
- Checksum-verified vendored upstream source replacement for credential-free,
  hermetic Pixi package builds.
- Mojo projections for UTF-8 length/copy string returns, presence-based
  optional values, and caller-owned mutable primitive spans.
- Process-global shared-library state that opens the library once and caches
  typed function pointers for all calls and opaque destructors.
- Decision records for private-registry packaging and global runtime state.

### Changed

- Opaque wrappers now retain only their Rust handle; library ownership and
  symbol resolution are shared process-wide.
- Binding guidance now covers fallible result carriers, read-only struct-tree
  views, mutable Diplomat opaques, Mojo 1.0 verification traps, and validated
  Apple-silicon macOS packaging.

[Unreleased]: https://github.com/sstadick/mojo-import-crates-skill/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sstadick/mojo-import-crates-skill/releases/tag/v0.1.0
