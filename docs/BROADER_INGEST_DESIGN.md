# Broader Ingest Design

Produced by the Plan subagent on 2026-04-19. Scope: generalize
`memu/ingest/` from "Python AST only" to "everything in a developer
environment becomes a typed wiki node." This is a design doc; no code
changes yet.

## 1. Taxonomy of source kinds

Grouped by ingestion difficulty — parser complexity plus cross-link
resolution cost, not file count.

- **Trivial (regex / line scan, <1 day each)**
  `.env.example`, `.gitignore`, `CODEOWNERS`, `LICENSE`, `.editorconfig`,
  `.prettierrc`, `.eslintrc` (JSON form), `Makefile` targets, `justfile`,
  `.tool-versions` / `.nvmrc` / `.python-version`, shell scripts
  (headers + function list only).

- **Easy (YAML / TOML / JSON + schema, <3 days each)**
  `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `Gemfile` /
  `Gemfile.lock`, `requirements.txt`, `composer.json`,
  `docker-compose.yml`, `Dockerfile` (directive list), GitHub Actions
  (`.github/workflows/*.yml`), CircleCI, GitLab CI, `tsconfig.json`,
  `renovate.json`, `netlify.toml`, `vercel.json`.

- **Medium (tree-sitter / existing libs, ~1 week each)**
  JS/TS, Go, Rust, Java, C#, Ruby, PHP, Swift, Kotlin, Scala, Lua,
  Elixir, Zig. Markdown docs (README, `docs/`, RFCs, ADRs) where we
  emit one node per heading and resolve intra-doc and code links.
  Kubernetes manifests (kind+name becomes the slug). Terraform HCL
  (the resource graph is the link structure).

- **Hard (multi-file semantic graph or opaque DSL, 2+ weeks)**
  C/C++ (preprocessor + headers), Gradle Groovy / Kotlin DSL, multi-
  module Maven, nginx / systemd (implicit ordering, includes),
  Bazel / Buck, proto / thrift / graphql schemas (cross-package types),
  OpenAPI specs with `$ref`. Git history as a time-series of commits
  with authorship + churn stats.

- **Needs-LLM (no grammar is enough)**
  Free-form CHANGELOG entries linked back to commits / issues,
  CONTRIBUTING "intent" extraction, ADR rationale summarization,
  release notes, inline TODO / FIXME triage, security advisories
  scraped from `SECURITY.md`, design-doc PDFs.

- **Peripheral but valuable** (often forgotten)
  Pre-commit hooks, `.devcontainer/devcontainer.json`, VS Code
  `.vscode/launch.json` + `tasks.json`, Nix flakes, Helm charts
  (values + templates), Ansible playbooks, dbt models (SQL +
  `schema.yml`), Jupyter notebooks (code cells become `code/` nodes,
  markdown becomes `doc/`), `Procfile`, `serverless.yml`, `cdk.json`.

## 2. `Source` Protocol

Generalize `LanguageParser` upward. A `LanguageParser` becomes *one
specific* `Source` (the code-symbol source). The orchestrator no
longer talks to parsers directly; it talks to Sources.

```python
@dataclass
class SourceFile:
    path: Path                # absolute
    rel_path: PurePosixPath   # relative to ingest root
    content: bytes            # already read, so Sources share I/O
    content_hash: str

@dataclass
class EmittedNode:
    slug: str                      # must be in a namespace this Source owns
    kind: NodeKind                 # "code" | "doc" | "config" | ...
    title: str
    body: str
    tags: list[str]
    source_meta: dict[str, Any]    # path, line range, commit, etc.
    references: list["SymbolicRef"]  # unresolved; resolver turns into LinkRecords
    content_hash: str              # for incremental skip

@dataclass(frozen=True)
class SymbolicRef:
    """A best-guess pointer. The global resolver decides if it becomes a link."""
    kind: Literal[
        "module", "symbol", "package", "config-key", "commit",
        "doc-heading", "file",
    ]
    target: str                    # e.g. "pkg:npm/react", "mod:pkg.sub", "file:src/a.ts"
    line: int = 0
    hint_type: LinkType = "related"

class Source(Protocol):
    name: str                      # "python-ast", "package-json", "github-actions"
    namespaces: tuple[str, ...]    # slug prefixes it may emit into

    def detect(self, file: SourceFile) -> bool: ...
    def enumerate(self, root: Path, walker: Walker) -> Iterable[SourceFile]: ...
    def parse(self, file: SourceFile) -> Iterable[EmittedNode]: ...
    def contribute_index(self, nodes: list[EmittedNode]) -> None: ...
        # optional: register exported symbols for cross-source lookup

class IngestResolver(Protocol):
    """Global; owns the symbol table across ALL Sources."""
    def register(self, source: Source, nodes: list[EmittedNode]) -> None: ...
    def resolve(self, ref: SymbolicRef, from_slug: str) -> Optional[LinkRecord]: ...
```

**Cross-source resolution.** The resolver keeps one flat
`target -> slug` table populated during a pre-pass. A `package.json`
dependency on `react` emits
`SymbolicRef(kind="package", target="npm:react")`. If `node_modules/react`
is also ingested (or a `pkg/npm/react` node already exists), the ref
resolves. A Go import of `github.com/foo/bar` emits
`SymbolicRef(kind="module", target="go:github.com/foo/bar")` which
resolves iff that repo is also on disk. A Kubernetes `Deployment`
referencing `ConfigMap: app-settings` resolves within the same ingest
pass via `config:k8s/configmap/app-settings`. The existing
`LanguageParser` + `LinkResolver` becomes a single `CodeSymbolSource`
implementation; the public `ingest_codebase` signature stays unchanged
(backward compat).

## 3. Slug namespaces

Keep existing (`code/`, `paper/`, `task/`, `note/`). Add:

- `pkg/<ecosystem>/<name>` — external dependencies as first-class nodes
  (`pkg/npm/react`, `pkg/pypi/fastapi`, `pkg/crates/serde`). Universal
  link target for manifests.
- `config/<tool>/<name>` — Dockerfiles, k8s kinds, Terraform resources,
  tsconfig, nginx server blocks (`config/docker/compose/web`,
  `config/k8s/deployment/api`).
- `doc/<rel-path>[#heading]` — prose from README / docs / RFCs / ADRs.
  One node per H1 / H2 so linking is precise.
- `vcs/commit/<sha>` and `vcs/author/<email-hash>` — git history as
  nodes; CHANGELOG entries link to commits.
- `ci/<provider>/<workflow>[/<job>]` — CI workflow graph
  (`ci/gha/release/publish`).
- `env/<name>` — environment-variable schema from `.env.example`; code
  nodes that call `os.getenv("FOO")` link here.
- `task/` already exists — reuse it for TODO / FIXME scrapes; separate
  from authored tasks via tags.

Each namespace is an obvious resolution target for one or more Sources,
avoids overloading `code/`, and lets vault_doctor enforce per-namespace
lifecycle rules (e.g. `pkg/*` may be pruned on dependency removal;
`doc/*` may not).

## 4. Priority-ordered roadmap (first 3 Sources)

Ship in this order; stop and review after each.

1. **JS/TS via tree-sitter** — **L**, ~1500 LOC.
   Unblocks the largest chunk of real-world repos. Emits `code/` nodes
   analogous to Python, plus `SymbolicRef(kind="package", ...)` for
   every `import` from `node_modules`.

2. **Manifest Source (`package.json` + `pyproject.toml` + `Cargo.toml` + `go.mod`)** — **M**, ~600 LOC.
   One Source, one parser dispatch per ecosystem. Immediately produces
   the `pkg/*` universe that Source #1 links into. Ship this *alongside*
   #1 so the first demo shows "your TS file links to `pkg/npm/react`."

3. **Docs Source (markdown in `README.md`, `docs/`, `adrs/`, `rfcs/`)** — **S**, ~400 LOC.
   Markdown is already well-understood; the win is that wiki-style
   `[[...]]` resolution across `code/`, `doc/`, `pkg/` makes the vault
   feel connected. Links from docs to code should resolve via prose
   mentions of known symbols (fuzzy, capped by confidence).

**Recommendation on tree-sitter: use `tree-sitter-languages` (Python
wheels).** It ships prebuilt grammars for ~40 languages in one pip dep,
no C toolchain at install time, works on macOS / Linux / Windows
wheels. Tradeoff: you inherit their grammar versions (occasionally lag
upstream) and the wheel is ~30 MB. Rejected alternatives: `tree-sitter`
+ manual grammar builds (breaks CI on machines without a C toolchain —
unacceptable for a library); `ctags` (no AST, only definitions — can't
resolve references); language servers (heavyweight, per-language
daemon, stateful — wrong shape for batch ingest). Gate tree-sitter
behind a `memu[ingest-ts]` extra so the core install stays slim.

## 5. Non-goals (v1)

- **Binary artifacts**: compiled `.so`, `.dll`, `.jar`, `.wasm`.
- **Minified / bundled output**: `dist/`, `build/`, `*.min.js` — already
  blocklisted by the walker.
- **Video / audio / images**: out of scope.
- **Proprietary IDE projects**: `.idea/`, `.vs/`, `*.xcodeproj/`, MSBuild `.sln`.
- **Closed-source vendored code**: we ingest what's in the tree; no remote fetching.
- **Live language servers**: no LSP bootstrapping at ingest time — we do
  static analysis only, matching the existing "purely lexical, no
  evaluation" doctrine from `memu/ingest/resolve.py`.
- **Lockfiles** (`package-lock.json`, `Cargo.lock`, `poetry.lock`):
  parsed only to enrich version fields on `pkg/*` nodes; no per-
  transitive-dep nodes in v1.
- **Git blame at symbol granularity**: commit-level only.

## 6. Open questions

1. **Secrets in `.env`**: do we ingest values at all, or only keys?
   Recommendation: keys only, always, even if the file is `.env` (not
   `.env.example`). Values never hit the vault. Needs sign-off.
2. **Git history**: re-ingest every commit on every run (O(history)
   writes), or snapshot-only at `HEAD` with a separate
   `ingest_git_history` pass that back-fills? Recommendation: snapshot
   at HEAD by default; history is a separate opt-in command with a
   `--since` window.
3. **Schemaless configs**: if we don't know a YAML file's schema (e.g.
   a random in-house tool), do we emit a `config/raw/<path>` node with
   the full body, or skip? Recommendation: emit, tagged `schemaless`,
   so it's searchable but doesn't pretend to have structure.
4. **Cross-repo `pkg/*` nodes**: if repo A is ingested and depends on
   `react`, do we create `pkg/npm/react` as a stub (like
   `resolve_or_stub`) or require that `react` itself also be ingested?
   Recommendation: stub eagerly — matches the existing dangling-link
   pattern in `memu/wiki/slug_registry.py`.
5. **LLM-enriched nodes**: should CHANGELOG summarization / ADR intent
   extraction run inline during ingest (slow, deterministic-ish), or
   async afterward via an agent pass? Lean toward post-hoc — ingest
   stays fast and reproducible.
6. **Deletion policy for generated namespaces**: `pkg/*` and `ci/*` are
   100 % derived. Does vault_doctor get permission to auto-delete stale
   ones, unlike the conservative rule for `code/`? Probably yes, but
   opt-in per namespace.
7. **Monorepo scoping**: for a repo with `packages/a`, `packages/b`,
   should slugs include a workspace prefix (`code/a/...`) or rely on
   package names from manifests? Recommendation: prefer manifest
   `name`, fall back to directory.

### Critical files for implementation

- `memu/ingest/codebase.py`
- `memu/ingest/parsers/base.py`
- `memu/ingest/parsers/__init__.py`
- `memu/ingest/resolve.py`
- `memu/wiki/slug_registry.py`
