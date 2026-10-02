# Isolated Client Qualification

This guide is for the public v3 candidate, not an upgrade of a running v2 store.
Build/install the candidate into a separate virtual environment and choose a new
absolute database path. Never reuse the operational memory database for a trial.
The examples below use placeholders, not a workstation's actual settings.

## What Was Tested

- Official MCP Python SDK 1.30.0 (`ClientSession`) and 2.3.0 (`Client`): real
  stdio initialization, tool discovery/schema parsing, two concurrent clients,
  revision conflicts, error recovery, restart and supersession.
- Codex CLI 0.159.2: isolated configuration, app-server discovery, a synthetic
  `handoff_save` and `memory_bootstrap` through `mcpServer/tool/call`. No model
  turn or real provider credential was used.
- Claude Code 2.1.281: isolated user configuration and `mcp get` connection
  health check. Tool invocation through a Claude model session was NOT tested.

These checks do not prove compatibility with every version, IDE/Desktop UI,
optional embedding model or provider. They are maintainer qualification on a
synthetic store, not independent adoption or a five-user pilot.

## Codex Recipe

Use a separate `CODEX_HOME` directory for the trial. In its `config.toml`, add:

```toml
[mcp_servers.memory_v3_trial]
command = "/absolute/path/to/trial-venv/bin/unified-memory-mcp"
startup_timeout_sec = 15
tool_timeout_sec = 30

[mcp_servers.memory_v3_trial.env]
UNIFIED_MEMORY_DB = "/absolute/path/to/new-trial/memory.db"
MEMORY_AUTOSYNC = "0"
MEMORY_ENABLE_EMBEDDINGS = "0"
```

On Windows use the absolute `trial-venv/Scripts/unified-memory-mcp.exe` path and
an absolute Windows database path; TOML literal single-quoted strings avoid
backslash escaping. Invoke `codex mcp list --json` in an empty trial workspace
under the isolated home and confirm only the intended trial server is present.
Listing configuration is not a connectivity/tool-call test by itself.

The maintainer's stronger probe used app-server initialize, an ephemeral thread,
`mcpServerStatus/list`, and direct `mcpServer/tool/call` requests. It never sent
`turn/start`; its model provider was pointed at an unused loopback address to
avoid using a real API. Normal interactive usage needs the client's usual
account/provider setup, which this project does not configure or provision.

## Claude Code Recipe

Use a separate `CLAUDE_CONFIG_DIR` and test home. Add the following JSON server
definition under a distinct name, for example `memory_v3_trial`, using
`claude mcp add-json --scope user <name> <json>`:

```json
{
  "type": "stdio",
  "command": "/absolute/path/to/trial-venv/bin/unified-memory-mcp",
  "args": [],
  "env": {
    "UNIFIED_MEMORY_DB": "/absolute/path/to/new-trial/memory.db",
    "MEMORY_AUTOSYNC": "0",
    "MEMORY_ENABLE_EMBEDDINGS": "0"
  }
}
```

Use the Windows executable/path equivalents on Windows. Run
`claude mcp get memory_v3_trial` in that same isolated environment to check the
server connection. A connected status is necessary but does not establish that
the model follows revision/task-selection instructions correctly.

No instructions here require copying tokens, private assistant memories or
production databases. Restore your normal environment variables when leaving
the trial; no real client profile needs editing for these checks.

## Cross-Client Trial

Use different `source`/`consumer` labels and the same explicit project/task ID:

1. Client A saves a synthetic task checkpoint and records its returned ID/revision.
2. Client B discovers that exact task without changing `resumed_by`.
3. Client B explicitly marks it resumed using ID and expected revision.
4. A deliberately stale update receives a conflict, not an overwrite.
5. After rereading, a deliberate completion succeeds. A fresh process no longer
   selects the completed task. No unrelated task is substituted.

Also test a missing task, ambiguous legacy checkpoints and an explicit zero
result limit. Do not send real private content merely to demonstrate continuity.

## Reproduce SDK Checks

Use separate environments for each SDK version. The SDK is a test-only dependency;
the memory server retains its zero mandatory third-party runtime dependencies.

```console
python -m pip install "mcp==1.30.0" .
python -m unittest discover -s tests_sdk -v
```

Repeat with `mcp==2.3.0` in a second environment. Tests block outbound socket
connections in the SDK process and disable optional imports/models in the child
server. They still start real local server processes and SQLite connections.

The server negotiates only its implemented MCP revision `2025-06-18`, rather
than echoing an arbitrary requested revision. SDK2 uses its normal auto mode
and can fall back from modern discovery to the supported legacy lifecycle.
No claim of implementing the newer stateless protocol is made.

## Sources

Official documentation consulted for setup and lifecycle (client versions above
were also checked against their own CLI help/schema):

```text
https://developers.openai.com/codex/mcp
https://developers.openai.com/codex/app-server
https://code.claude.com/docs/en/settings
https://github.com/modelcontextprotocol/python-sdk
https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
```
