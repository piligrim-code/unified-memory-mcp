import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import server


class PublicDefaultsTest(unittest.TestCase):
    def test_private_store_is_not_available(self):
        self.assertFalse(hasattr(server, "_load_sealed_session"))
        self.assertFalse(hasattr(server, "sealed_records"))
        self.assertEqual(server._sealed_hits("anything", 5, None), [])
        self.assertEqual(server.DEFAULT_CLAUDE_MEM_GLOB, "")
        self.assertEqual(server.SERVER_VERSION, "3.0.0rc1")

    def test_gateway_requires_explicit_workspace(self):
        path = Path(server.__file__).with_name("gateway.py")
        run = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 2)
        self.assertIn("--workspace-root", run.stderr)

    def test_no_implicit_import_from_local_assistant_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            imports = root / ".claude/projects/synthetic/memory"
            imports.mkdir(parents=True)
            (imports / "fixture.md").write_text("PRIVATE_SYNTHETIC_SENTINEL", encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("UNIFIED_MEMORY_", "MEMORY_", "CLAUDE_MEM"))}
            env.update({"HOME": tmp, "USERPROFILE": tmp,
                        "UNIFIED_MEMORY_DB": str(root / "test.db")})
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "memory_list", "arguments": {}}},
            ]
            run = subprocess.run([sys.executable, server.__file__],
                                 input="".join(json.dumps(m) + "\n" for m in messages),
                                 capture_output=True, text=True, env=env, timeout=20)
            self.assertEqual(run.returncode, 0)
            replies = [json.loads(line) for line in run.stdout.splitlines()]
            self.assertEqual(len(replies), 2)
            self.assertNotIn("error", replies[1])
            self.assertNotIn("PRIVATE_SYNTHETIC_SENTINEL", run.stdout)
            self.assertNotIn("autosync:", run.stderr)


if __name__ == "__main__":
    unittest.main()
