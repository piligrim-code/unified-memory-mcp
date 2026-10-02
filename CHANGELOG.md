# Changelog

## 3.0.0rc1 - Unreleased

Breaking API candidate; not yet a package-registry or production release.

- Startup requires an explicit absolute database path; no implicit home-store
  selection or accidental migration from an unconfigured launch.
- Negotiate only the implemented MCP protocol revision, expose cross-tool client
  instructions, and refuse startup if database initialization fails. Add official
  SDK1/SDK2 interoperability checks and isolated client/pilot guidance.

- Read-only task/session/ID-scoped handoff discovery, explicit ambiguity and
  staleness handling, deterministic tie ordering and disabled zero-limit sections.
- Bootstrap memory defaults to the project scope instead of a global search.
- Portable project keys replace silent filesystem-basename normalization.
- Existing-record writes require revisions; stale updates, deletes, completion
  and resume marking return conflicts instead of overwriting concurrent work.
- Additive transactional database migration; future schema versions refused.
- Explicit supersession removes obsolete records from recall without erasing
  audit history. Recall includes timestamp, source, revision and match method.
- Read-only snapshot diagnostics compare SQLite, FTS and chunk content.
- Project JSON export and preview-first import into new stores, never overwrite.
- Synthetic two-client process demo, failure/migration/concurrency tests and
  installed-wheel qualification on the declared CI matrix.

See docs/continuity-v3.md for client changes, backup-first upgrade and rollback.
Old writers must not remain connected to a migrated database. No live service,
user database, private import configuration or model download is activated.
