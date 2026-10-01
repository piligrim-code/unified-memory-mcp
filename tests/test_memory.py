import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import gateway
import server
from memory_admin import (
    backup,
    deployment_preflight,
    inspect_database,
    load_deployment_profile,
    profile_environment,
    recovery_drill,
    restart_drill,
    restore_copy,
    safe_absolute_path,
    verify_backup,
    write_default_deployment_profile,
    write_preflight_report,
)


class IsolatedStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ["UNIFIED_MEMORY_DB"] = str(Path(self.tmp.name) / "memory.db")
        os.environ["MEMORY_AUTOSYNC"] = "0"
        server._conn = None
        server._embedder = False

    def tearDown(self):
        if server._conn is not None:
            server._conn.close()
        server._conn = None
        server._embedder = None
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def test_server_version_matches_release(self):
        self.assertEqual("2.5.1", server.SERVER_VERSION)

    def test_handoff_round_trip_and_session_upsert(self):
        saved = server.tool_handoff_save({
            "project": r"mcp-memory",
            "session_id": "claude-session-1",
            "source": "claude",
            "target": "codex",
            "task": "Add cross-client continuity",
            "summary": "Schema and tools are in progress.",
            "completed_work": ["schema"],
            "files": ["server.py"],
            "next_steps": ["tests"],
        })
        self.assertTrue(saved["created"])
        handoff_id = saved["handoff"]["id"]

        updated = server.tool_handoff_save({
            "project": "mcp-memory",
            "session_id": "claude-session-1",
            "source": "claude",
            "target": "codex",
            "summary": "Schema, tools, and tests are complete.",
            "completed_work": ["schema", "tools", "tests"],
            "next_steps": ["wire client instructions"],
        })
        self.assertFalse(updated["created"])
        self.assertEqual(handoff_id, updated["handoff"]["id"])
        self.assertEqual("Add cross-client continuity", updated["handoff"]["task"])

        loaded = server.tool_handoff_load({
            "project": "mcp-memory", "consumer": "codex"
        })
        self.assertEqual(1, loaded["count"])
        self.assertEqual("codex", loaded["handoffs"][0]["resumed_by"])
        self.assertEqual(["schema", "tools", "tests"],
                         loaded["handoffs"][0]["completed_work"])

        server.tool_memory_save({
            "content": "mcp-memory keeps durable shared context",
            "source": "test",
        })
        bootstrap = server.tool_memory_bootstrap({
            "project": "mcp-memory", "consumer": "codex", "query": "shared context"
        })
        self.assertEqual(1, bootstrap["handoff"]["count"])
        self.assertGreaterEqual(bootstrap["memories"]["count"], 1)

        completed = server.tool_handoff_complete({"id": handoff_id, "notes": "done"})
        self.assertTrue(completed["completed"])
        self.assertEqual(0, server.tool_handoff_load({
            "project": "mcp-memory", "consumer": "codex"
        })["count"])

    def test_autosync_discovers_all_claude_project_memory_dirs(self):
        projects = Path(self.tmp.name) / ".claude" / "projects"
        for slug, body in (("d--proj", "main memory"), ("d--proj-other", "other memory")):
            memory_dir = projects / slug / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "same-name.md").write_text(body, encoding="utf-8")
            (memory_dir / "MEMORY.md").write_text("index", encoding="utf-8")
        os.environ.pop("CLAUDE_MEM_DIR", None)
        os.environ["CLAUDE_MEM_GLOB"] = str(projects / "*" / "memory")

        result = server.autosync()
        rows = server.conn().execute(
            "SELECT content,tags FROM memories WHERE source='claude' ORDER BY content"
        ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual(2, len(result["changed"]))
        self.assertIn("claude-project:d--proj", rows[0]["tags"] + rows[1]["tags"])
        self.assertIn("claude-project:d--proj-other", rows[0]["tags"] + rows[1]["tags"])

    def test_database_connections_are_thread_local_for_http_gateway(self):
        server.conn()
        result = {}

        def worker():
            try:
                result["value"] = server.tool_memory_save({"content": "written in request thread"})
            except Exception as exc:
                result["error"] = exc
            finally:
                server.close_thread_connection()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertTrue(result["value"]["saved"])

    def test_scope_allowlist_filters_reads_and_blocks_mutation(self):
        hidden = server.tool_memory_save({"content": "hidden memory", "scope": "hidden"})
        os.environ["UNIFIED_MEMORY_ALLOWED_SCOPES"] = "team-a"
        visible = server.tool_memory_save({"content": "visible memory", "scope": "team-a"})

        with self.assertRaisesRegex(PermissionError, "not authorized"):
            server.tool_memory_save({"content": "blocked", "scope": "team-b"})
        with self.assertRaisesRegex(PermissionError, "not authorized"):
            server.tool_memory_get({"id": hidden["id"]})
        with self.assertRaisesRegex(PermissionError, "not authorized"):
            server.tool_memory_update({"id": hidden["id"], "content": "rewrite"})
        with self.assertRaisesRegex(PermissionError, "not authorized"):
            server.tool_memory_delete({"id": hidden["id"]})

        listed = server.tool_memory_list({})
        searched = server.tool_memory_search({"query": "memory"})
        recalled = server.tool_memory_recall({"query": "memory"})
        policy = server.tool_memory_policy({})

        self.assertEqual([visible["id"]], [item["id"] for item in listed["results"]])
        self.assertEqual({"team-a"}, {item["scope"] for item in searched["results"]})
        self.assertEqual({"team-a"}, {item["scope"] for item in recalled["results"]})
        self.assertEqual("scoped-single-tenant", policy["mode"])
        self.assertEqual(["team-a"], policy["authorization"]["allowed_scopes"])
        self.assertEqual(["team-a"], [item["scope"] for item in policy["usage"]["by_scope"]])

    def test_record_and_byte_quotas_fail_closed(self):
        os.environ["UNIFIED_MEMORY_MAX_MEMORIES"] = "1"
        os.environ["UNIFIED_MEMORY_MAX_BYTES"] = "5"
        saved = server.tool_memory_save({"content": "abc"})

        with self.assertRaisesRegex(PermissionError, "record quota"):
            server.tool_memory_save({"content": "x"})
        with self.assertRaisesRegex(PermissionError, "byte quota"):
            server.tool_memory_update({"id": saved["id"], "content": "abcdef"})

        updated = server.tool_memory_update({"id": saved["id"], "content": "12345"})
        policy = server.tool_memory_policy({})
        self.assertTrue(updated["updated"])
        self.assertEqual(1, policy["usage"]["records"])
        self.assertEqual(5, policy["usage"]["content_bytes"])

    def test_retention_prune_requires_policy_and_confirmation(self):
        old = server.tool_memory_save({"content": "old", "scope": "global"})
        current = server.tool_memory_save({"content": "current", "scope": "global"})
        server.conn().execute(
            "UPDATE memories SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (old["id"],),
        )
        server.conn().commit()

        with self.assertRaisesRegex(PermissionError, "retention is disabled"):
            server.tool_memory_prune({"confirm": True})
        os.environ["UNIFIED_MEMORY_RETENTION_DAYS"] = "30"
        with self.assertRaisesRegex(PermissionError, "confirm=true"):
            server.tool_memory_prune({"confirm": False})

        result = server.tool_memory_prune({"confirm": True})
        self.assertEqual(1, result["pruned"])
        self.assertFalse(server.tool_memory_get({"id": old["id"]})["found"])
        self.assertTrue(server.tool_memory_get({"id": current["id"]})["found"])

    def test_handoff_project_allowlist_and_id_ownership(self):
        hidden = server.tool_handoff_save({
            "project": "hidden-project", "source": "claude", "summary": "hidden"
        })
        os.environ["UNIFIED_MEMORY_ALLOWED_PROJECTS"] = "mcp-memory"
        allowed = server.tool_handoff_save({
            "project": r"mcp-memory", "source": "codex", "summary": "allowed"
        })

        self.assertEqual("mcp-memory", allowed["handoff"]["project"])
        with self.assertRaisesRegex(PermissionError, "not authorized"):
            server.tool_handoff_load({"project": "hidden-project", "consumer": "codex"})
        with self.assertRaisesRegex(PermissionError, "not authorized"):
            server.tool_handoff_complete({"id": hidden["handoff"]["id"]})
        with self.assertRaisesRegex(PermissionError, "project/source"):
            server.tool_handoff_save({
                "id": allowed["handoff"]["id"],
                "project": "mcp-memory",
                "source": "claude",
                "summary": "wrong owner",
            })

    def test_handoff_quota_and_completed_retention(self):
        os.environ["UNIFIED_MEMORY_MAX_HANDOFFS"] = "2"
        completed = server.tool_handoff_save({
            "project": "mcp-memory", "source": "codex", "summary": "completed"
        })
        active = server.tool_handoff_save({
            "project": "mcp-memory", "source": "claude", "summary": "active",
            "status": "active",
        })
        with self.assertRaisesRegex(PermissionError, "handoff record quota"):
            server.tool_handoff_save({
                "project": "mcp-memory", "source": "other", "summary": "overflow"
            })

        server.tool_handoff_complete({"id": completed["handoff"]["id"]})
        server.conn().execute(
            "UPDATE handoffs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id IN (?,?)",
            (completed["handoff"]["id"], active["handoff"]["id"]),
        )
        server.conn().commit()
        os.environ["UNIFIED_MEMORY_HANDOFF_RETENTION_DAYS"] = "30"

        pruned = server.tool_memory_prune({"confirm": True})
        remaining = server.conn().execute("SELECT id,status FROM handoffs").fetchall()

        self.assertEqual(0, pruned["memory_pruned"])
        self.assertEqual(1, pruned["handoffs_pruned"])
        self.assertEqual([(active["handoff"]["id"], "active")], [tuple(row) for row in remaining])


class WorkspaceGatewayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        gateway.configure_workspace(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_guarded_read_write_replace_delete(self):
        created = gateway.tool_workspace_write({"path": "repo/file.txt", "content": "one\ntwo\n"})
        read = gateway.tool_workspace_read({"path": "repo/file.txt"})
        self.assertEqual(created["sha256"], read["sha256"])
        self.assertEqual("one\ntwo\n", read["content"])

        with self.assertRaisesRegex(ValueError, "expected_sha256"):
            gateway.tool_workspace_write({"path": "repo/file.txt", "content": "blind overwrite"})

        replaced = gateway.tool_workspace_replace({
            "path": "repo/file.txt", "old": "two", "new": "three",
            "expected_sha256": read["sha256"],
        })
        self.assertEqual("one\nthree\n", gateway.tool_workspace_read({"path": "repo/file.txt"})["content"])
        gateway.tool_workspace_delete({
            "path": "repo/file.txt", "expected_sha256": replaced["sha256"]
        })
        self.assertFalse((Path(self.tmp.name) / "repo" / "file.txt").exists())

    def test_workspace_path_cannot_escape_root(self):
        with self.assertRaisesRegex(ValueError, "escapes"):
            gateway.resolve_workspace_path("../outside.txt")

    def test_gateway_lists_memory_and_workspace_tools(self):
        response = gateway.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("memory_bootstrap", names)
        self.assertIn("memory_policy", names)
        self.assertIn("memory_prune", names)
        self.assertIn("handoff_save", names)
        self.assertIn("workspace_replace", names)


class MemoryAdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "memory.db"
        self.old_db = os.environ.get("UNIFIED_MEMORY_DB")
        os.environ["UNIFIED_MEMORY_DB"] = str(self.db)
        server._conn = None
        server.tool_memory_save({"content": "durable test memory", "source": "test"})

    def tearDown(self):
        if server._conn is not None:
            server._conn.close()
        server._conn = None
        if self.old_db is None:
            os.environ.pop("UNIFIED_MEMORY_DB", None)
        else:
            os.environ["UNIFIED_MEMORY_DB"] = self.old_db
        self.tmp.cleanup()

    def _profile(self, **limit_overrides):
        limits = {
            "max_records": 100,
            "max_content_bytes": 1024 * 1024,
            "retention_days": 365,
            "max_handoffs": 50,
            "handoff_retention_days": 90,
        }
        limits.update(limit_overrides)
        path = self.root / "deployment-profile.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "mode": "scoped-single-tenant",
            "allowed_scopes": ["global", "mcp-memory"],
            "allowed_projects": ["mcp-memory"],
            "limits": limits,
        }), encoding="utf-8")
        return path

    def test_online_backup_manifest_verify_and_restore_copy(self):
        backup_path, manifest_path = backup(
            self.db, self.root / "backups" / "memory.db"
        )

        result = verify_backup(backup_path, manifest_path)
        restored = restore_copy(
            backup_path, manifest_path, self.root / "restored" / "memory.db"
        )

        self.assertTrue(result["verified"])
        self.assertTrue(inspect_database(restored)["ok"])
        connection = sqlite3.connect(restored)
        try:
            count = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)

    def test_backup_verification_rejects_tampering_and_overwrite(self):
        backup_path, manifest_path = backup(self.db, self.root / "backup.db")
        backup_path.write_bytes(backup_path.read_bytes() + b"tamper")

        with self.assertRaisesRegex(ValueError, "size|SHA-256"):
            verify_backup(backup_path, manifest_path)
        with self.assertRaisesRegex(ValueError, "overwrite"):
            backup(self.db, backup_path)

    def test_recovery_drill_reconnects_stdio_without_persisting_content(self):
        server.tool_handoff_save({
            "project": "mcp-memory",
            "source": "test",
            "summary": "private recovery fixture summary",
        })

        report, report_path = recovery_drill(self.db, self.root / "recovery-drill")
        encoded = report_path.read_text(encoding="utf-8")

        self.assertEqual("PASS", report["verdict"])
        self.assertTrue(all(report["checks"].values()))
        self.assertEqual({"memories": 1, "handoffs": 1}, {
            "memories": report["restore"]["counts"]["memories"],
            "handoffs": report["restore"]["counts"]["handoffs"],
        })
        self.assertFalse(report["content_included"])
        self.assertNotIn("durable test memory", encoded)
        self.assertNotIn("private recovery fixture summary", encoded)
        with self.assertRaisesRegex(ValueError, "must be empty"):
            recovery_drill(self.db, self.root / "recovery-drill")

    def test_restart_drill_preserves_policy_across_two_stdio_processes(self):
        report, report_path = restart_drill(self.root / "restart-drill")
        encoded = report_path.read_text(encoding="utf-8")

        self.assertEqual("PASS", report["verdict"])
        self.assertTrue(all(report["checks"].values()))
        self.assertEqual(2, report["processes"])
        self.assertEqual(1, report["database"]["counts"]["memories"])
        self.assertEqual(0, report["database"]["counts"]["handoffs"])
        self.assertFalse(report["content_included"])
        self.assertNotIn("synthetic restart drill marker", encoded)
        with self.assertRaisesRegex(ValueError, "must be empty"):
            restart_drill(self.root / "restart-drill")

    def test_bounded_deployment_profile_maps_to_exact_server_environment(self):
        profile = load_deployment_profile(self._profile())

        self.assertEqual(["global", "mcp-memory"], profile["allowed_scopes"])
        self.assertEqual({
            "UNIFIED_MEMORY_ALLOWED_PROJECTS": "mcp-memory",
            "UNIFIED_MEMORY_ALLOWED_SCOPES": "global,mcp-memory",
            "UNIFIED_MEMORY_HANDOFF_RETENTION_DAYS": "90",
            "UNIFIED_MEMORY_MAX_BYTES": "1048576",
            "UNIFIED_MEMORY_MAX_HANDOFFS": "50",
            "UNIFIED_MEMORY_MAX_MEMORIES": "100",
            "UNIFIED_MEMORY_RETENTION_DAYS": "365",
        }, profile_environment(profile))

    def test_default_deployment_profile_is_bounded_and_refuses_overwrite(self):
        output = write_default_deployment_profile(self.root / "generated-profile.json")

        profile = load_deployment_profile(output)
        shipped = load_deployment_profile(
            Path(__file__).resolve().parents[1]
            / "profiles"
            / "single-tenant-bounded.json"
        )

        self.assertEqual("scoped-single-tenant", profile["mode"])
        self.assertTrue(all(profile["limits"].values()))
        self.assertEqual(shipped, profile)
        with self.assertRaisesRegex(ValueError, "must not exist"):
            write_default_deployment_profile(output)

    def test_deployment_profile_rejects_unbounded_and_unknown_settings(self):
        with self.assertRaisesRegex(ValueError, "within"):
            load_deployment_profile(self._profile(max_records=0))
        value = json.loads(self._profile().read_text(encoding="utf-8"))
        value["unexpected"] = True
        invalid = self.root / "invalid-profile.json"
        invalid.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing or unknown"):
            load_deployment_profile(invalid)

    def test_deployment_preflight_is_content_free_and_report_refuses_overwrite(self):
        profile_path = self._profile()
        boundary = {"platform": "test", "no_broad_access": True}
        with mock.patch("memory_admin.inspect_access_boundary", return_value=boundary):
            report = deployment_preflight(
                profile_path,
                self.db,
                self.root / "backups",
            )
        report_path = write_preflight_report(report, self.root / "preflight.json")
        encoded = report_path.read_text(encoding="utf-8")

        self.assertEqual("PASS", report["verdict"])
        self.assertTrue(all(report["checks"].values()))
        self.assertFalse(report["content_included"])
        self.assertNotIn("durable test memory", encoded)
        with self.assertRaisesRegex(ValueError, "must not exist"):
            write_preflight_report(report, report_path)

    def test_database_inspection_rejects_linked_paths(self):
        link = self.root / "memory-link.db"
        try:
            link.symlink_to(self.db)
        except OSError:
            self.skipTest("symlink creation unavailable")

        with self.assertRaisesRegex(ValueError, "symlink or junction"):
            safe_absolute_path(link)
        with self.assertRaisesRegex(ValueError, "symlink or junction"):
            inspect_database(link)


if __name__ == "__main__":
    unittest.main()
