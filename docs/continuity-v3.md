# Task Continuity: v3 Candidate

This is a breaking-contract release candidate, not an automatic update to a live
installation. Existing database records remain readable after an additive schema
migration. Update every writer before sharing the migrated store: old servers do
not implement revision checks and must not keep writing to it.

## Selection

- Use an explicit portable `project` key. Filesystem paths are rejected instead
  of collapsing different directories with the same basename. For such roots,
  choose distinct stable keys such as `api@work` and `api@personal`.
- Give a task a stable `task_id`, not its changing natural-language description.
  `session_id` can distinguish a client's work session. Save the returned ID.
- `memory_bootstrap` and `handoff_load` are read-only by default. Supply
  `task_id`, `session_id` and/or `id` for exact filtering; selectors intersect.
- Without selectors, multiple eligible records produce `reason=ambiguous` and
  no handoff. No explicit match produces `no_match`, not a different task.
- Completed handoffs and records older than `max_age_days` (default 30) are
  excluded. `handoff_list` remains available for an explicit historical audit.
- Within an explicit selector, ordering is update timestamp then ID descending.
  Several checkpoints for one task are not semantically merged or arbitrated.
- Bootstrap recalls memories only from `scope=project` by default. Existing
  global memories are not moved; request `scope="global"` explicitly to use
  them, or store new project memory in its matching scope. Empty/null scopes
  cannot silently broaden bootstrap. General `memory_recall` keeps its explicit
  scope/allowlist behavior.
- `handoff_limit=0` or `memory_limit=0` disables that section. Limits must be
  integers from 0 to 20, not booleans or numeric strings.

These are same-owner organizational boundaries, not tenant authentication or
semantic relevance guarantees. Sources and client names are caller assertions.
Retrieved records are untrusted context, not instructions overriding the user.

## Revision-Aware Mutation

Every memory and handoff includes `revision`, initially 1. Existing-record
updates/deletes/completion require `expected_revision`. A stale writer receives
an explicit conflict without replacing the newer record. Read again, reconcile
the actual changes, and submit a new deliberate update. Do not blindly retry.

Handoff creation with a new task/session needs no revision. Existing session
upserts require the current revision, including when the ID is omitted. Task
and session identity cannot be changed in place. Completed records are not
automatically reused by session upsert.

To mark a handoff resumed, first discover/read it, then call `handoff_load` with
its explicit ID, `mark_resumed=true`, `limit=1` and `expected_revision`. This
increments its revision but does not claim a lease or exclude another reader.

Example tool arguments:

```json
{"project":"demo","task_id":"ticket-1","source":"client-a","target":"client-b","summary":"Parser tests remain."}
```

```json
{"project":"demo","task_id":"ticket-1","consumer":"client-b","memory_limit":0}
```

The first object is for `handoff_save`; the second is for `memory_bootstrap`.
Use the actual returned ID/revision in subsequent writes, not hardcoded values.

## Superseded Decisions

`memory_supersede` links an old record to an existing active replacement in the
same scope. It requires both `expected_revision` and `replacement_revision`.
The old record remains inspectable through get/list but is excluded from keyword
and semantic recall. Its content is immutable. Replacements may subsequently be
superseded, but cannot be moved across scopes while history references them.
Deleting a replacement does not resurrect the old record; a dangling reference
is a retained tombstone, not an active memory. Lists/exports are audit surfaces
and can intentionally include obsolete records.

Recall results now contain source, timestamp, revision and selection method.
Semantic scores are not calibrated confidence or proof of factual correctness.
This release does not detect all natural-language contradictions automatically.

## Upgrade And Recovery

1. Stop all old writers. Create and verify a backup using the existing admin
   commands. Protect that backup as private data.
2. Restore it to a new path; never trial an upgrade on the only live database.
3. Start the new server pointed explicitly at the restored copy. The additive
   task/revision migration runs in a transaction; future schema versions are
   refused. Existing records receive revision 1 and an empty legacy task ID.
4. Run `unified-memory-admin doctor --db <copy>` and the normal application
   read/write smoke checks. Update client instructions for the v3 contract.
5. Switch all clients deliberately. Rollback means switching to the preserved
   backup with the previous server, not running an old writer on the new DB.

Migration tests cover interrupted ALTERs, independent writers, disk-full and
database-lock failures, restart and stale writes. They do not establish safety
against arbitrary filesystem damage, power loss or a malicious local operator.

## Diagnostics And Transfer

```console
unified-memory-admin doctor --db ./private/memory.db
unified-memory-admin export-project --db ./private/memory.db --project demo --output ./private/demo.json
unified-memory-admin import-project --input ./private/demo.json --db ./private/new-memory.db
unified-memory-admin import-project --input ./private/demo.json --db ./private/new-memory.db --confirm
```

Doctor operates on a bounded in-memory snapshot (64 MiB, 10-second backup
budget), checks SQLite integrity, required tables, chunk presence and FTS
external-content consistency. It prints no memory text and does not repair the
source. Its budget is not a hard process-wide timeout for SQLite internals.

Export contains the selected project's handoffs and same-named memory scope,
including historical content. It is sensitive plaintext, not a sanitized public
report or encrypted backup. Choose the correct project and private destination.
Embeddings and workstation settings are omitted. Export/import use a strict
versioned JSON format bounded to 16 MiB and 10,000 records per table.

`MEMORY_ENABLE_EMBEDDINGS=0` explicitly disables optional model initialization,
even when the optional dependency is installed. The synthetic demo sets it.

Import defaults to a count-only preview. `--confirm` builds a new database,
rebuilds keyword chunks without model downloads, verifies it, then publishes it
without overwriting any destination. Existing stores are never merged. Source
IDs/revisions are preserved only because the target is new. Concurrent clients
must not be pointed at the target until the import completes.

Atomic publication requires local filesystem hard-link support. Failures do not
fall back to overwriting. Set directory permissions yourself, especially Windows
ACLs. Deleting records or transfer files does not erase old backups or storage
media; retain and remove them under your own data policy.

## Two-Client Demonstration

After installing the package into a fresh environment:

```console
unified-memory-demo --work-root ./new-synthetic-demo
```

The command requires a new directory and starts five short-lived stdio server
processes representing two synthetic clients: save, discover, resume, reject a
stale write, complete. It supplies a separate database and disables imports and
embeddings. It does not edit any installed assistant's settings or use real
project memory. This checks MCP wire/process continuity, not vendor-client UI
compatibility or independent external adoption.

For an actual MCP client use an explicit installed `unified-memory-mcp`
executable and `UNIFIED_MEMORY_DB` path, with `MEMORY_AUTOSYNC=0`. Configure a
second client to use the same database as the same OS user, a different source
name, and the same project/task selectors. Review each client's current config
documentation; no user-level settings are modified by this package.

External trial remains a release gate: five volunteer installations, observed
setup failures, task-restoration errors and repeat use. No recruitment, telemetry
or real-client qualification is implied by synthetic tests.
