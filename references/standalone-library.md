# Standalone Mojo library mode

Read this file in full only when the requested Rust binding must become a new,
reusable Mojo library repository. The ordinary existing-project workflow does
not load or apply this mode.

## Boundary

Standalone mode creates a product around one newly generated binding. It does
not relax any source-resolution, semantic-projection, ABI, provenance, or test
requirement from `SKILL.md`. A successful binding is necessary but is not a
finished standalone library.

Use this mode when the user explicitly asks for a library/repository, or when
the supplied target is otherwise empty and the user clearly wants reusable
output. Do not replace or scaffold over a directory that already contains
`pixi.toml`; use existing-project mode there.

The repository shell is generic infrastructure. Never use a finished binding
repository as a source template for another crate. Derive the public facade,
tests, examples, README claims, safety notes, and guide content from the exact
resolved crate and reviewed `binding.toml`.

## Decisions before scaffolding

Resolve or obtain these inputs before writing the target:

- root package name and Mojo import identifier;
- root package version;
- exact Rust package name and resolved version;
- one exact Mojo compiler version supported by the generated binding;
- intended platforms, limited to `linux-64`, `linux-aarch64`, and `osx-arm64`;
- optional authors;
- optional `OWNER/REPOSITORY` coordinates and default branch, if already known;
- the standard AI-generation disclosure, omitted only when the user explicitly
  requests omission.

Do not infer a license. Ask the user before adding one. Repository coordinates
configure links, source references, and an optional Pages deployment job; they
do not authorize `git init`, remote creation, pushes, pull requests, repository
settings, or Pages settings.

Normalize a hyphenated package name to a valid underscore-separated Mojo import
only when that mapping is unambiguous. Ask when the public name is a product
decision rather than a mechanical normalization.

## Create the repository shell

Resolve the Rust source outside the target first, then invoke:

```text
python3 <skill-root>/scripts/scaffold_standalone_library.py \
  --project <new-or-empty-directory> \
  --package-name <pixi-package-name> \
  --mojo-package <mojo_import_name> \
  --package-version <root-version> \
  --rust-crate <resolved-rust-package> \
  --rust-version <resolved-rust-version> \
  --mojo-version <exact-mojo-version> \
  [--author "Name <email>"] \
  [--github-repository OWNER/REPOSITORY] \
  [--default-branch main] \
  [--omit-ai-disclosure]
```

Repeat `--author` and `--platform` when needed. With no `--platform`, the
scaffold declares all three supported targets. The command accepts a missing
target directory or a directory containing only `.git`; it refuses any other
pre-existing content and never initializes Git.

The scaffold is a valid starting point, not a finished API. It contains a
documented minimal Mojo function in a normal submodule, smoke tests, an example,
a downstream Git consumer, provenance checker, CI, and docs infrastructure.
The submodule is intentional: Modo 0.11.13 omits functions implemented directly
in `__init__.mojo`, while documenting re-exported functions from normal modules.
After the binding is integrated, remove the scaffold function and replace every
proof with real public operations before finishing the API-specific deliverables
below.

The reusable shell includes approximately:

```text
pixi.toml
README.md
CHANGELOG.md
<mojo-package>/__init__.mojo
<mojo-package>/scaffold.mojo
tests/test_import.mojo
examples/import.mojo
scripts/check_generated.py
ci/run_examples.py
ci/test_git_dependency.py
ci/git-consumer/
.github/workflows/ci.yml
.github/workflows/docs.yml
modo.yaml
docs/
```

Use publish artifact mode for a modern standalone repository so the root and
aggregate Rust-binding source packages are both publishable. Run the normal
integrator after scaffolding; it owns the narrow additions to root
`pixi.toml` and the complete `vendor/rust-bindings/` tree.

## Public facade and tests

The generated native Mojo package remains an implementation detail. Author an
ordinary root package that imports it and exposes names suitable for downstream
users. Preserve ownership and error semantics approved during projection.

Before completion:

- replace the scaffold package docstring with a package overview and a
  complete primary example;
- implement documented public functions and types in normal `.mojo` modules,
  then re-export them from `__init__.mojo`; Modo 0.11.13 does not produce API
  pages or doctests for functions implemented directly in `__init__.mojo`;
- keep Modo export processing enabled and list public symbols in the package
  docstring as `module.symbol` entries; `__init__.symbol` does not resolve;
- add structured docstrings to every public alias, constant, type, constructor,
  method, function, argument, return, and raised error;
- mark internal/native constructors and implementation helpers `@doc_hidden`;
- replace `tests/test_import.mojo` with public-facade tests covering a successful
  representative workflow and at least one meaningful error or edge path;
- prove imports without `-I`; and
- keep raw/native package names out of downstream examples unless the raw layer
  is intentionally public.

Use the installed `mojo-syntax` skill for all Mojo authoring and compile every
new example and test with the project-pinned compiler.

## Standalone examples

Delete the scaffold's trivial `examples/import.mojo` once real examples exist.
Provide at least two small standalone programs representing the principal
operations users perform. Do not force universal "read" and "write" examples
onto crates whose APIs have different task boundaries.

Examples must:

- import only the public root package;
- contain current `def main()` syntax and explicit `raises` when needed;
- be self-contained when invoked with no arguments;
- avoid network access and persistent writes outside their working directory;
- print enough output for a user to understand the result; and
- be run by `python3 ci/run_examples.py` in isolated temporary directories.

If a useful CLI also accepts arguments, keep its zero-argument path deterministic
for CI and document the optional arguments. Do not call an example verified
merely because it formats or compiles; execute it against the installed package.

## Downstream Git dependency proof

The generic consumer initially proves that the root package can execute its
scaffold function through a Git dependency. Replace that call with one small
real public operation after the facade exists. Keep the consumer independent
of project-relative `-I` paths and declare the Git dependency in all three
places needed by a packaged downstream library:

- `[package.build-dependencies]`;
- `[package.run-dependencies]`; and
- `[dependencies]` for local tests.

`ci/test_git_dependency.py` renders the fixture against a local repository URL
and immutable revision, installs it in a temporary checkout, and runs its test.
Because an uncommitted working tree is not part of a Git revision, run this
proof on a commit or supply an explicit committed revision.

## README and changelog

The scaffold README contains accurate source identity and development commands,
but it is intentionally minimal. Rewrite it from the reviewed public API before
completion. Put the useful path first:

1. AI-generated repository disclosure linked to the skill source, unless the
   user explicitly opted out;
2. documentation-site link near the top when repository coordinates exist;
3. exact Rust crate/source identity, supported operations, feature limits where
   relevant, and actually tested platforms;
4. one-command Git installation:
   `pixi add --git "<url>" <package> && pixi install`;
5. an application dependency example pinned to a branch for approachability and
   a full immutable `rev` for reproducibility;
6. the three dependency declarations required when another
   `pixi-build-mojo` package consumes the library;
7. a complete public-facade Mojo example;
8. a concise supported-API table and important safety/ownership behavior;
9. commands for standalone examples, tests, provenance checks, docs, and
   artifact publication; and
10. provenance/regeneration notes that link to the skill without claiming the
    generated repository is a reusable binding template.

Keep commands copyable and tested. Label the site as documentation, guides, and
examples rather than only "API documentation" when it contains all three.

Maintain `CHANGELOG.md` in Keep a Changelog form. Record the initial binding,
public facade, examples, CI, documentation, and deferred limitations under
`Unreleased` until the user selects a release date/version. Do not fabricate a
release or tag.

## Documentation site

The scaffold provides Modo/Hugo infrastructure modeled on the current
ExtraMojo documentation stack. It enforces strict cross-references and compiler
diagnostics for missing public docstrings.

Finish these content layers:

- an example-first homepage with installation, one complete copyable standalone
  program, task-to-guide/API navigation, and prominent safety limits;
- a quickstart;
- at least two task-oriented guides derived from the actual API;
- focused fenced `mojo` examples in public docstrings; and
- generated reference pages for every public root-package type and method.

Visible standalone programs should be one copyable block. The bundled Modo
template treats a global-only doctest as a complete module and executes it
verbatim; grouped snippets are wrapped with the current Mojo `TestSuite`
runner. Use hidden doctest code only for setup/assertions that would distract
from the reader. Give every doctest an underscore-separated Mojo identifier;
Modo incorporates the label into a generated test function name.

Require all of the following:

- `mojo doc --diagnose-missing-doc-strings --Werror` succeeds;
- Modo strict mode reports 100% public docstring coverage;
- every extracted documentation example executes;
- Hugo renders a non-empty homepage plus guide and API routes;
- generated links contain no source `.md` suffixes; and
- a browser-width visual pass confirms installation and a real example are
  visible from the landing page without relying on sidebar discovery.

When GitHub coordinates are unavailable, keep the local base URL and omit the
Pages deployment job. Add hosted source links and deployment only after the
coordinates are known. Revalidate the pinned Modo checksum, Hugo revision, Go
version, and action SHAs when deliberately upgrading the docs toolchain.

## CI and hosted boundaries

The scaffold CI matrix covers the selected supported platforms, verifies the
binding provenance, checks formatting, runs facade tests and examples, proves a
downstream Git dependency on Linux x86-64, builds both publishable packages, and
uploads artifacts. Do not claim a platform was tested merely because its matrix
entry was generated.

The docs workflow builds on pull requests and deploys from the configured
default branch only when GitHub coordinates were supplied. Its least-privilege
default is read-only contents; only the deploy job receives write permission.

Workflow generation is a local file operation. Creating the hosted repository,
enabling Actions or Pages, pushing, opening a pull request, merging, and changing
branch/repository settings are separate external mutations requiring the user's
request. If asked to publish, watch both CI and docs workflows through a terminal
state and inspect the served Pages content after deployment rather than relying
only on a green build.

## Completion checklist

Run the commands appropriate to the selected Pixi version and repository:

```text
pixi install --frozen
pixi run format
git diff --exit-code -- <formatted-mojo-paths>
pixi run check-generated
pixi run t
python3 ci/run_examples.py
python3 ci/test_git_dependency.py --platform <native-platform>
pixi publish --target-dir <temporary-artifact-directory>
modo build
hugo --source docs/site --minify
python3 <skill-root>/scripts/integrate_binding.py ... --check
```

Also rerun the binding's Rust tests, external-prefix artifact installation, and
direct installed-binary checks required by the core workflow. `pixi install
--frozen` is a clean-checkout assertion only after `pixi.lock` has been created
and intentionally updated.

Inspect the published aggregate binding payload. It must not contain
`share/max/cache/.mojo_cache`, `share/max/crashdb`, or
`share/max/firstActivation`; those are compiler state, not library content. The
current `pixi-build-mojo` backend may independently add a zero-byte
`share/max/firstActivation` marker to the root Mojo artifact. Report that
backend-owned marker when observed, but do not allow it in the generated
aggregate binding artifact.

A standalone run is complete only when the repository contains no scaffold-only
import examples/tests, no unintended placeholders (the downstream fixture's
runtime `__PROJECT_GIT__`, `__PROJECT_REV__`, and `__CI_PLATFORM__` markers are
expected), no untracked build products, and no claims unsupported by an
executed test or generated provenance. Report any intentionally deferred
hosting, license, platform, API, or documentation work explicitly.
