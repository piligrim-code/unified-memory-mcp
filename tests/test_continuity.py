"""Task selection and conflict safety using only disposable synthetic stores."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import server


class StoreCase(unittest.TestCase):
    def setUp(self):
        server.close_thread_connection()
        self.tmp = tempfile.TemporaryDirectory()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('UNIFIED_MEMORY_', 'MEMORY_', 'CLAUDE_MEM'))}
        env.update(UNIFIED_MEMORY_DB=str(Path(self.tmp.name) / 'test.db'), MEMORY_AUTOSYNC='0')
        self.environment = mock.patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.embedding = mock.patch.object(server, '_embedder', False)
        self.embedding.start()

    def tearDown(self):
        server.close_thread_connection()
        self.embedding.stop()
        self.environment.stop()
        self.tmp.cleanup()

    def save(self, **kwargs):
        return server.tool_handoff_save(dict(project='sample', source='author',
                                             summary='Synthetic checkpoint', **kwargs))['handoff']

    def load(self, **kwargs):
        return server.tool_handoff_load(dict(project='sample', consumer='reader', **kwargs))


class ContinuityTest(StoreCase):
    def test_new_revision_and_explicit_task(self):
        saved = self.save(task_id='one')
        self.assertEqual((1, 'one'), (saved['revision'], saved['task_id']))

    def test_discovery_has_no_resume_side_effect(self):
        saved = self.save()
        result = self.load()
        self.assertEqual('selected', result['reason'])
        self.assertEqual('', result['handoffs'][0]['resumed_by'])
        self.assertEqual(1, result['handoffs'][0]['revision'])

    def test_explicit_mark_resumed_requires_revision(self):
        saved = self.save()
        with self.assertRaisesRegex(ValueError, 'expected_revision'):
            self.load(id=saved['id'], mark_resumed=True)
        result = self.load(id=saved['id'], mark_resumed=True, expected_revision=1)
        self.assertEqual('reader', result['handoffs'][0]['resumed_by'])
        self.assertEqual(2, result['handoffs'][0]['revision'])

    def test_missing_task_never_falls_back(self):
        self.save(task_id='one')
        self.assertEqual(0, self.load(task_id='two')['count'])

    def test_different_tasks_are_ambiguous_without_selector(self):
        self.save(task_id='one')
        self.save(task_id='two')
        result = self.load()
        self.assertEqual((0, 'ambiguous'), (result['count'], result['reason']))

    def test_legacy_multiple_rows_are_ambiguous(self):
        self.save(session_id='one')
        self.save(session_id='two')
        self.assertEqual('ambiguous', self.load()['reason'])

    def test_explicit_task_selects_only_that_task(self):
        first = self.save(task_id='one')
        self.save(task_id='two')
        self.assertEqual(first['id'], self.load(task_id='one')['handoffs'][0]['id'])

    def test_exact_session_selector(self):
        first = self.save(session_id='one')
        self.save(session_id='two')
        self.assertEqual(first['id'], self.load(session_id='one')['handoffs'][0]['id'])

    def test_selectors_are_intersected(self):
        first = self.save(task_id='one', session_id='session')
        for selector in ({'id': first['id'], 'task_id': 'wrong'},
                         {'id': first['id'], 'session_id': 'wrong'}):
            with self.subTest(selector=selector):
                self.assertEqual(0, self.load(**selector)['count'])

    def test_same_task_recency_ties_use_id(self):
        with mock.patch.object(server, 'now_iso', return_value='2026-10-03T00:00:00+00:00'):
            first = self.save(task_id='one')
            second = self.save(task_id='one')
        self.assertGreater(second['id'], first['id'])
        self.assertEqual(second['id'], self.load(task_id='one')['handoffs'][0]['id'])

    def test_wrong_project_and_target_and_own_source_are_excluded(self):
        self.save(target='someone-else')
        server.tool_handoff_save(dict(project='other', source='author', summary='Other'))
        server.tool_handoff_save(dict(project='sample', source='reader', summary='Own'))
        self.assertEqual(0, self.load()['count'])
        self.assertEqual(1, self.load(include_own=True)['count'])

    def test_completed_and_stale_checkpoints_are_excluded(self):
        first = self.save(status='completed')
        second = self.save()
        with server.conn() as connection:
            connection.execute('UPDATE handoffs SET updated_at=? WHERE id=?',
                               ('2000-01-01T00:00:00+00:00', second['id']))
        self.assertEqual(0, self.load()['count'])
        self.assertEqual(0, self.load(id=first['id'])['count'])

    def test_invalid_task_ids_fail(self):
        for value in ('', '  ', None, 1, [], 'x' * 201, 'x\n'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.save(task_id=value)

    def test_path_project_names_are_not_silently_collapsed(self):
        for project in ('C:/one/repo', 'D:/two/repo', '../repo'):
            with self.subTest(project=project), self.assertRaises(ValueError):
                server.tool_handoff_load(dict(project=project, consumer='reader'))

    def test_explicit_zero_limits_are_disabled(self):
        self.save()
        with mock.patch.object(server, 'tool_memory_recall', side_effect=AssertionError('recall disabled')):
            result = server.tool_memory_bootstrap(dict(project='sample', consumer='reader',
                                                       handoff_limit=0, memory_limit=0))
        self.assertEqual('disabled', result['handoff']['reason'])
        self.assertEqual(0, result['memories']['count'])

    def test_invalid_limits_fail_before_recall(self):
        for name in ('handoff_limit', 'memory_limit'):
            for value in (-1, True, '2', 21):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    server.tool_memory_bootstrap(dict(project='sample', consumer='reader', **{name: value}))

    def test_bootstrap_defaults_to_project_memory_scope(self):
        server.tool_memory_save(dict(content='synthetic query result', source='test', scope='other'))
        server.tool_memory_save(dict(content='synthetic query result', source='test', scope='sample'))
        result = server.tool_memory_bootstrap(dict(project='sample', consumer='reader', query='synthetic'))
        self.assertEqual({'sample'}, {r['scope'] for r in result['memories']['results']})

    def test_explicit_empty_scope_does_not_broaden_bootstrap(self):
        for scope in (None, '', ' '):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                server.tool_memory_bootstrap(dict(project='sample', consumer='reader', scope=scope))

    def test_update_preserves_original_target(self):
        row = self.save(target='reader')
        result = server.tool_handoff_save(dict(project='sample', source='author', id=row['id'],
                                               expected_revision=1, summary='New'))
        self.assertEqual('reader', result['handoff']['target'])

    def test_stale_revision_returns_conflict_without_overwrite(self):
        row = self.save()
        updated = server.tool_handoff_save(dict(project='sample', source='author', id=row['id'],
                                               expected_revision=1, summary='New'))
        self.assertEqual(2, updated['handoff']['revision'])
        result = server.tool_handoff_save(dict(project='sample', source='author', id=row['id'],
                                              expected_revision=1, summary='Stale'))
        self.assertEqual((False, 'conflict'), (result['saved'], result['reason']))
        self.assertEqual('New', self.load()['handoffs'][0]['summary'])

    def test_update_requires_revision(self):
        row = self.save()
        with self.assertRaisesRegex(ValueError, 'expected_revision'):
            server.tool_handoff_save(dict(project='sample', source='author', id=row['id'], summary='Blind'))

    def test_task_identity_cannot_be_changed_in_place(self):
        row = self.save(task_id='one')
        with self.assertRaisesRegex(ValueError, 'task_id'):
            server.tool_handoff_save(dict(project='sample', source='author', id=row['id'],
                                         expected_revision=1, task_id='two', summary='Changed'))

    def test_session_upsert_is_task_scoped_and_revision_checked(self):
        first = self.save(task_id='one', session_id='shared')
        second = self.save(task_id='two', session_id='shared')
        self.assertNotEqual(first['id'], second['id'])
        with self.assertRaisesRegex(ValueError, 'expected_revision'):
            self.save(task_id='one', session_id='shared')

    def test_no_autocreation_on_stale_update(self):
        with self.assertRaises(ValueError):
            self.save(expected_revision=4, session_id='missing')
        self.assertEqual(0, self.load()['count'])

    def test_complete_is_revision_checked(self):
        row = self.save()
        result = server.tool_handoff_complete(dict(id=row['id'], expected_revision=2))
        self.assertEqual('conflict', result['reason'])
        result = server.tool_handoff_complete(dict(id=row['id'], expected_revision=1))
        self.assertTrue(result['completed'])
        self.assertEqual(0, self.load()['count'])

    def test_competing_writers_have_one_winner(self):
        row = self.save()
        barrier = threading.Barrier(2)
        def write(value):
            try:
                server.conn()
                barrier.wait(timeout=10)
                return server.tool_handoff_save(dict(project='sample', source='author', id=row['id'],
                                                     expected_revision=1, summary=value))
            finally:
                server.close_thread_connection()
        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(write, 'A')
            b = executor.submit(write, 'B')
            results = [a.result(timeout=20), b.result(timeout=20)]
        self.assertEqual(1, sum(r['saved'] for r in results))
        self.assertEqual(2, self.load()['handoffs'][0]['revision'])

    def test_failure_rolls_back_and_next_write_succeeds(self):
        with mock.patch.object(server, '_enforce_handoff_quota', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                self.save()
        self.assertFalse(server.conn().in_transaction)
        self.assertEqual(1, self.save()['revision'])

    def test_restart_preserves_task_and_revision(self):
        row = self.save(task_id='one')
        server.close_thread_connection()
        loaded = self.load(task_id='one')['handoffs'][0]
        self.assertEqual((row['id'], 1), (loaded['id'], loaded['revision']))

    def test_wire_schema_exposes_new_arguments(self):
        schemas = {t['name']: t['inputSchema']['properties'] for t in server.TOOLS}
        for tool in ('handoff_save', 'handoff_load', 'memory_bootstrap'):
            with self.subTest(tool=tool):
                self.assertIn('task_id', schemas[tool])
        for tool in ('handoff_save', 'handoff_complete', 'memory_update', 'memory_delete'):
            with self.subTest(tool=tool):
                self.assertIn('expected_revision', schemas[tool])


if __name__ == '__main__':
    unittest.main()
