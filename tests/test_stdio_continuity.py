import builtins
from contextlib import closing
import json
import os
import subprocess
import sys
import sqlite3
from pathlib import Path
from unittest import mock

import memory_demo
import server
from test_continuity import StoreCase


class StdioContinuityTest(StoreCase):
    def test_initialize_negotiates_only_supported_revision(self):
        for requested in ('2025-06-18', '2025-11-25', '2099-01-01', None):
            with self.subTest(requested=requested):
                result = server.handle_initialize({'protocolVersion': requested})
                self.assertEqual(server.DEFAULT_PROTOCOL, result['protocolVersion'])
                self.assertIn('revision', result['instructions'])

    def test_conditional_writes_are_not_advertised_read_only(self):
        tools = {t['name']: t for t in server.TOOLS}
        self.assertTrue(tools['memory_get']['annotations']['readOnlyHint'])
        for name in ('memory_bootstrap', 'handoff_load', 'handoff_save', 'memory_delete'):
            with self.subTest(name=name):
                self.assertFalse(tools[name]['annotations']['readOnlyHint'])

    def test_invalid_database_cannot_advertise_healthy_startup(self):
        database = Path(self.tmp.name) / 'future.db'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('PRAGMA user_version=999')
        before = database.read_bytes()
        env = dict(os.environ, UNIFIED_MEMORY_DB=str(database))
        result = subprocess.run([sys.executable, server.__file__], env=env,
                                input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}) + '\n',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(1, result.returncode)
        self.assertEqual('', result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(before, database.read_bytes())

    def test_unconfigured_startup_cannot_open_home_database(self):
        for value in (None, 'relative.db'):
            env = dict(os.environ, HOME=self.tmp.name, USERPROFILE=self.tmp.name)
            env.pop('UNIFIED_MEMORY_DB', None)
            if value is not None:
                env['UNIFIED_MEMORY_DB'] = value
            with self.subTest(value=value):
                result = subprocess.run([sys.executable, server.__file__], input='', env=env,
                                        capture_output=True, text=True, timeout=10, cwd=self.tmp.name)
                self.assertEqual(2, result.returncode)
                self.assertIn('UNIFIED_MEMORY_DB', result.stderr)
                self.assertEqual([], list(Path(self.tmp.name).iterdir()))

    def test_two_clients_and_five_real_processes(self):
        result = memory_demo.demo(Path(self.tmp.name) / 'demo')
        self.assertTrue(result['ok'])
        self.assertEqual((2, 5), (result['clients'], result['processes']))
        self.assertTrue(result['stale_write_rejected'])
        self.assertNotIn('Synthetic checkpoint', json.dumps(result))
        with self.assertRaises(FileExistsError):
            memory_demo.demo(Path(self.tmp.name) / 'demo')

    def test_model_import_disabled_even_if_installed(self):
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name == 'fastembed':
                raise AssertionError('Optional model import attempted')
            return original(name, *args, **kwargs)
        with mock.patch.dict(os.environ, {'MEMORY_ENABLE_EMBEDDINGS': '0'}), \
                mock.patch.object(server, '_embedder', None), \
                mock.patch('builtins.__import__', side_effect=guarded):
            self.assertIsNone(server.get_embedder())

    def test_protocol_exposes_revision_and_returns_conflict(self):
        database = Path(self.tmp.name) / 'protocol.db'
        saved = memory_demo.call(database, 'memory_save', dict(content='synthetic', scope='sample'))
        self.assertEqual(1, saved['revision'])
        updated = memory_demo.call(database, 'memory_update', dict(id=saved['id'], content='updated', expected_revision=1))
        self.assertEqual(2, updated['revision'])
        conflict = memory_demo.call(database, 'memory_update', dict(id=saved['id'], content='stale', expected_revision=1))
        self.assertEqual('conflict', conflict['reason'])

    def test_missing_protocol_revision_fails_without_change(self):
        database = Path(self.tmp.name) / 'protocol.db'
        saved = memory_demo.call(database, 'memory_save', dict(content='synthetic', scope='sample'))
        with self.assertRaises(RuntimeError):
            memory_demo.call(database, 'memory_update', dict(id=saved['id'], content='blind'))
        record = memory_demo.call(database, 'memory_get', dict(id=saved['id']))['memory']
        self.assertEqual(('synthetic', 1), (record['content'], record['revision']))
