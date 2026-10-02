"""Transactions, migration and offline transfer on synthetic temporary databases."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest import mock

import server
from memory_transfer import doctor, export_project, import_project, load_export
from test_continuity import StoreCase


class DurabilityTest(StoreCase):
    def memory(self, content='Synthetic context', scope='sample'):
        return server.tool_memory_save(dict(content=content, scope=scope, source='fixture'))

    def path(self, name):
        return Path(self.tmp.name) / name

    def test_memory_revision_rejects_stale_update_and_delete(self):
        saved = self.memory()
        result = server.tool_memory_update(dict(id=saved['id'], content='New', expected_revision=1))
        self.assertEqual(2, result['revision'])
        for method, key in ((server.tool_memory_update, 'updated'), (server.tool_memory_delete, 'deleted')):
            with self.subTest(key=key):
                result = method(dict(id=saved['id'], content='Old', expected_revision=1))
                self.assertFalse(result[key])
                self.assertEqual('conflict', result['reason'])

    def test_superseded_memory_is_visible_for_audit_not_recall(self):
        old, new = self.memory('Synthetic old'), self.memory('Synthetic new')
        result = server.tool_memory_supersede(dict(id=old['id'], replacement_id=new['id'],
                                                  expected_revision=1, replacement_revision=1))
        self.assertTrue(result['superseded'])
        for method in (server.tool_memory_search, server.tool_memory_recall):
            with self.subTest(method=method.__name__):
                rows = method(dict(query='Synthetic', scope='sample'))['results']
                self.assertEqual(1, len(rows))
        self.assertEqual(new['id'], server.tool_memory_get(dict(id=old['id']))['memory']['superseded_by'])
        self.assertEqual(2, server.tool_memory_list(dict(scope='sample'))['count'])

    def test_supersession_rejects_cross_scope_cycles_and_stale_replacement(self):
        old, new, other = self.memory(), self.memory(), self.memory(scope='other')
        for target in (old, other):
            with self.subTest(target=target['id']), self.assertRaises(ValueError):
                server.tool_memory_supersede(dict(id=old['id'], replacement_id=target['id'],
                                                  expected_revision=1, replacement_revision=1))
        result = server.tool_memory_supersede(dict(id=old['id'], replacement_id=new['id'],
                                                  expected_revision=1, replacement_revision=9))
        self.assertEqual('replacement_conflict', result['reason'])
        server.tool_memory_supersede(dict(id=old['id'], replacement_id=new['id'],
                                         expected_revision=1, replacement_revision=1))
        with self.assertRaises(ValueError):
            server.tool_memory_supersede(dict(id=new['id'], replacement_id=old['id'],
                                             expected_revision=1, replacement_revision=2))

    def test_deleted_replacement_does_not_resurrect_old_memory(self):
        old, new = self.memory('Synthetic old'), self.memory('Synthetic new')
        server.tool_memory_supersede(dict(id=old['id'], replacement_id=new['id'],
                                         expected_revision=1, replacement_revision=1))
        server.tool_memory_delete(dict(id=new['id'], expected_revision=1))
        self.assertEqual(0, server.tool_memory_recall(dict(query='Synthetic'))['count'])
        export = self.path('deleted-reference.json')
        export_project(self.path('test.db'), export, 'sample')
        self.assertTrue(import_project(export, self.path('new.db'), confirm=True)['imported'])

    def test_referenced_replacement_cannot_change_scope(self):
        old, new = self.memory(), self.memory()
        server.tool_memory_supersede(dict(id=old['id'], replacement_id=new['id'],
                                         expected_revision=1, replacement_revision=1))
        with self.assertRaisesRegex(ValueError, 'scope'):
            server.tool_memory_update(dict(id=new['id'], scope='other', expected_revision=1))

    def test_doctor_detects_stale_chunk_text(self):
        self.memory()
        with server.conn() as c:
            c.execute("UPDATE chunks SET text='stale synthetic text'")
        self.assertFalse(doctor(self.path('test.db'))['checks']['chunk_text_matches'])

    def test_recall_includes_provenance_and_explanation(self):
        self.memory()
        result = server.tool_memory_recall(dict(query='Synthetic', scope='sample'))['results'][0]
        self.assertEqual(('fixture', 'keyword_match', 1), (result['source'], result['reason'], result['revision']))
        self.assertIn('updated_at', result)

    def test_empty_update_does_not_destroy_content(self):
        row = self.memory()
        with self.assertRaises(ValueError):
            server.tool_memory_update(dict(id=row['id'], content=' ', expected_revision=1))
        self.assertEqual(1, server.tool_memory_get(dict(id=row['id']))['memory']['revision'])

    def test_semantic_recall_preserves_scope_and_excludes_superseded(self):
        with mock.patch.object(server, '_embed', side_effect=lambda texts, kind: [[1.0, 0.0] for _ in texts]):
            old, new = self.memory('Old'), self.memory('New')
            self.memory('Other', 'other')
            server.tool_memory_supersede(dict(id=old['id'], replacement_id=new['id'],
                                             expected_revision=1, replacement_revision=1))
            results = server.tool_memory_recall(dict(query='Query', scope='sample'))['results']
        self.assertEqual([new['id']], [r['memory_id'] for r in results])
        self.assertEqual('semantic_similarity', results[0]['reason'])

    def test_unembedded_rows_are_not_reported_as_semantic_matches(self):
        self.memory()
        with mock.patch.object(server, '_embed', return_value=[[1.0, 0.0]]):
            self.assertEqual(0, server.tool_memory_recall(dict(query='Query', scope='sample'))['count'])

    def test_failed_chunk_write_rolls_back_whole_memory(self):
        self.memory()
        with mock.patch.object(server, '_index_memory', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                self.memory('Failed insert')
        self.assertEqual(1, server.tool_memory_list({})['count'])
        self.assertFalse(server.conn().in_transaction)
        self.assertTrue(self.memory('Successful retry')['saved'])

    def test_failed_memory_update_preserves_text_and_revision(self):
        saved = self.memory()
        with mock.patch.object(server, '_index_memory', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                server.tool_memory_update(dict(id=saved['id'], content='Changed', expected_revision=1))
        result = server.tool_memory_get(dict(id=saved['id']))['memory']
        self.assertEqual(('Synthetic context', 1), (result['content'], result['revision']))
        self.assertTrue(doctor(self.path('test.db'))['ok'])

    def test_locked_database_leaves_no_half_checkpoint(self):
        server.conn().execute('PRAGMA busy_timeout=20')
        with closing(sqlite3.connect(self.path('test.db'))) as locker:
            locker.execute('BEGIN IMMEDIATE')
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    self.save()
            finally:
                locker.rollback()
        self.assertEqual(0, self.load()['count'])
        self.assertEqual(1, self.save()['revision'])

    def test_full_database_rolls_back(self):
        c = server.conn()
        before = c.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        pages = c.execute('PRAGMA page_count').fetchone()[0]
        c.execute('PRAGMA max_page_count=' + str(pages))
        with self.assertRaises(sqlite3.DatabaseError):
            self.memory('large synthetic input ' * 20000)
        self.assertEqual(before, c.execute('SELECT COUNT(*) FROM memories').fetchone()[0])
        self.assertFalse(c.in_transaction)
        c.execute('PRAGMA max_page_count=1000000')
        self.assertTrue(self.memory()['saved'])

    def test_migration_preserves_legacy_rows_and_adds_defaults(self):
        path = self.path('legacy.db')
        with closing(sqlite3.connect(path)) as c, mock.patch.object(server, '_migrate_schema'):
            server._init_schema(c)
            c.execute("INSERT INTO memories(content,created_at,updated_at) VALUES('legacy','2026-10-03','2026-10-03')")
            c.commit()
        with closing(sqlite3.connect(path)) as c:
            server._init_schema(c)
            self.assertEqual(('legacy', 1, None), c.execute('SELECT content,revision,superseded_by FROM memories').fetchone())
            self.assertEqual(1, c.execute('PRAGMA user_version').fetchone()[0])

    def test_interrupted_migration_rolls_back_columns(self):
        path = self.path('legacy.db')
        with closing(sqlite3.connect(path)) as c, mock.patch.object(server, '_migrate_schema'):
            server._init_schema(c)
        with closing(sqlite3.connect(path)) as c:
            alters = []
            def authorize(action, arg1, arg2, database, trigger):
                if action == sqlite3.SQLITE_ALTER_TABLE:
                    alters.append(arg2)
                    if len(alters) == 2:
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            c.set_authorizer(authorize)
            with self.assertRaises(sqlite3.DatabaseError):
                server._migrate_schema(c)
            c.set_authorizer(None)
            self.assertNotIn('task_id', {row[1] for row in c.execute('PRAGMA table_info(handoffs)')})
            self.assertEqual(0, c.execute('PRAGMA user_version').fetchone()[0])
            server._migrate_schema(c)
            self.assertEqual(1, c.execute('PRAGMA user_version').fetchone()[0])

    def test_future_schema_is_refused_without_creation(self):
        with closing(sqlite3.connect(self.path('future.db'))) as c:
            c.execute('PRAGMA user_version=999')
            with self.assertRaises(ValueError):
                server._init_schema(c)
            self.assertEqual([], c.execute('SELECT name FROM sqlite_master').fetchall())

    def test_doctor_is_content_free_and_detects_fts_corruption(self):
        self.memory('PRIVATE_SYNTHETIC_SENTINEL')
        good = doctor(self.path('test.db'))
        self.assertTrue(good['ok'])
        self.assertNotIn('PRIVATE_SYNTHETIC_SENTINEL', json.dumps(good))
        with server.conn() as c:
            c.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")
        report = doctor(self.path('test.db'))
        self.assertFalse(report['ok'])
        self.assertFalse(report['checks']['fts_content_matches'])
        self.assertFalse(server.tool_memory_recall(dict(query='PRIVATE'))['results'])

    def test_doctor_missing_database_does_not_create_it(self):
        with self.assertRaises(ValueError):
            doctor(self.path('missing.db'))
        self.assertFalse(self.path('missing.db').exists())

    def test_project_export_preview_and_new_database_round_trip(self):
        self.memory()
        self.memory('Other project private fixture', 'other')
        handoff = self.save(task_id='one', session_id='first')
        export = self.path('project.json')
        export_project(self.path('test.db'), export, 'sample')
        self.assertNotIn('Other project private', export.read_text())
        preview = import_project(export, self.path('imported.db'))
        self.assertEqual((1, 1, False), (preview['memories'], preview['handoffs'], preview['imported']))
        self.assertFalse(self.path('imported.db').exists())
        self.assertTrue(import_project(export, self.path('imported.db'), confirm=True)['imported'])
        self.assertTrue(doctor(self.path('imported.db'))['ok'])
        with closing(sqlite3.connect(self.path('imported.db'))) as c:
            self.assertEqual(('one', 1), c.execute('SELECT task_id,revision FROM handoffs').fetchone())

    def test_transfer_refuses_existing_outputs(self):
        self.memory()
        export = self.path('project.json')
        export_project(self.path('test.db'), export, 'sample')
        original = export.read_bytes()
        with self.assertRaises(FileExistsError):
            export_project(self.path('test.db'), export, 'sample')
        self.assertEqual(original, export.read_bytes())
        with self.assertRaises(ValueError):
            import_project(export, self.path('test.db'), confirm=True)

    def test_untrusted_transfer_rejects_scope_ids_json_and_cycles(self):
        old, new = self.memory(), self.memory()
        export = self.path('project.json')
        export_project(self.path('test.db'), export, 'sample')
        original = json.loads(export.read_text())
        for case in ('scope', 'id', 'extra', 'cycle'):
            value = json.loads(json.dumps(original))
            if case == 'scope':
                value['memories'][0]['scope'] = 'other'
            elif case == 'id':
                value['memories'][0]['id'] = True
            elif case == 'extra':
                value['memories'][0]['sql'] = 'untrusted'
            else:
                value['memories'][0]['superseded_by'] = value['memories'][1]['id']
                value['memories'][1]['superseded_by'] = value['memories'][0]['id']
            export.write_text(json.dumps(value))
            with self.subTest(case=case), self.assertRaises(ValueError):
                import_project(export, self.path('never.db'), confirm=True)
            self.assertFalse(self.path('never.db').exists())
        for raw in ('{"version":1,"version":2}', '{"x":NaN}'):
            export.write_text(raw)
            with self.assertRaises(ValueError):
                load_export(export)

    def test_failed_import_never_publishes_database(self):
        self.memory()
        export = self.path('project.json')
        export_project(self.path('test.db'), export, 'sample')
        with mock.patch('memory_transfer.doctor', return_value={'ok': False}):
            with self.assertRaises(ValueError):
                import_project(export, self.path('never.db'), confirm=True)
        self.assertFalse(self.path('never.db').exists())
        self.assertEqual([], list(Path(self.tmp.name).glob('.memory-import-*')))

    def test_admin_errors_do_not_echo_untrusted_content(self):
        path = self.path('bad.json')
        path.write_text('PRIVATE_SYNTHETIC_SENTINEL')
        result = subprocess.run([sys.executable, '-m', 'memory_admin', 'import-project', '--input',
                                 str(path), '--db', str(self.path('never.db'))],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(1, result.returncode)
        self.assertNotIn('PRIVATE_SYNTHETIC_SENTINEL', result.stdout + result.stderr)
