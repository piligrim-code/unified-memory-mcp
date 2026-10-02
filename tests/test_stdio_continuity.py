import builtins
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import memory_demo
import server
from test_continuity import StoreCase


class StdioContinuityTest(StoreCase):
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
