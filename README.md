# MCP Memory

## v3 continuity candidate

The current branch prepares `3.0.0rc1`: exact task selection, read-only bootstrap,
revision-aware writes, superseded decisions, content-free diagnostics and explicit
project transfer. **Existing writers must be updated** before using the migrated
store. Read [the migration and continuity contract](docs/continuity-v3.md) and
[release gates](docs/release-checklist.md). Nothing upgrades a live installation
or imports private memory automatically.

See [isolated client setup and tested boundaries](docs/client-setup.md) and the
[voluntary pilot checklist](docs/pilot-checklist.md). Official MCP SDK checks
are separate from the dependency-free core tests.

Local, single-tenant memory and handoffs for MCP clients.

The core uses Python's standard library and SQLite FTS5: no account, hosted
service or runtime dependency is needed. Optional embeddings are a separate
extra and may download model weights; the default installation uses text search.

## Install

Python 3.11 or newer; candidate qualification uses Python 3.12.

```sh
python -m venv .venv
# Activate .venv using your platform's standard command.
python -m pip install .
python -m unittest discover -s tests -v
unified-memory-admin --help
```

The distribution name is `unified-memory-mcp`. Version 3.0.0rc1 is a candidate,
not a claim that a package was uploaded. The historical 2.5.1 public import is
preserved in Git history; its source manifest is provenance, not a v3 checksum.

## Connect a stdio client

Use your MCP client's configuration syntax, with explicit executable and database
paths. For a client accepting a mcpServers object, the shape is:

```json
{
  "mcpServers": {
    "memory": {
      "command": "/absolute/path/to/.venv/bin/unified-memory-mcp",
      "env": {
        "UNIFIED_MEMORY_DB": "/absolute/private/path/memory.db",
        "MEMORY_AUTOSYNC": "0"
      }
    }
  }
}
```

On Windows the installed executable is under .venv/Scripts.
The same user can point multiple local MCP processes at one SQLite store.
Do not share a database between mutually untrusted users or tenants.

Tools include memory_save/search/recall, memory_bootstrap, handoff_save/load,
memory_policy and explicit-confirmation pruning. Scopes organize records;
they are not user identities or RBAC boundaries.

## Public-safe defaults

- No local assistant memory is imported automatically.
- Explicit import needs MEMORY_AUTOSYNC=1 and CLAUDE_MEM_DIR or CLAUDE_MEM_GLOB
  pointing to a reviewed directory. Importing private text is your decision.
- Personal private-store integration and the workstation-specific sync utility
  are not included.
- The optional HTTP gateway requires --workspace-root explicitly. It offers
  authenticated file operations as well as memory; use stdio when those
  capabilities are unnecessary.
- HTTP is for a trusted loopback/tunnel deployment, not public Internet exposure.
  Configure MEMORY_GATEWAY_TOKEN or MEMORY_GATEWAY_TOKEN_FILE; do not commit it.
- No real database, token, deployment settings, autostart task or SSH material
  is shipped or activated.
- The history-free snapshot contains no workstation paths, personal assistant
  memory, private records, database files, tokens, raw logs or private settings.

The source manifest records original/exported hashes and AST transformations.
The private live server is not modified by building this snapshot.

## Backup and restart check

Use only a fresh synthetic directory for a first test:

```sh
unified-memory-admin restart-drill --work-root ./restart-demo
unified-memory-admin write-deployment-profile --output ./deployment-profile.json
```

Backups and restore-copy require explicit paths; restore-copy never overwrites
an existing destination. Protect database and backup permissions yourself.
The strict deployment preflight includes Windows-specific storage/ACL checks;
do not infer Linux ACL qualification from portable stdio tests.

See SECURITY.md. This candidate is released under MIT; third-party dependencies
and optional model weights retain their own terms.
