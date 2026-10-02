"""Explicit offline inspection and project-scoped transfer, never live DB discovery."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

import server
from memory_admin import safe_absolute_path

MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 10000
MEMORY_FIELDS = {'id', 'content', 'tags', 'source', 'scope', 'created_at', 'updated_at',
                 'revision', 'superseded_by', 'superseded_at'}
HANDOFF_FIELDS = {'id', 'project', 'task_id', 'session_id', 'source', 'target', 'status', 'task',
                  'summary', 'notes', 'metadata', 'created_at', 'updated_at', 'resumed_at',
                  'resumed_by', 'revision', *server.HANDOFF_ARRAY_FIELDS}


def _readonly(path):
    path = safe_absolute_path(Path(path))
    if not path.is_file():
        raise ValueError('Database does not exist')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    return connection


def doctor(path):
    """Inspect a bounded in-memory SQLite backup; never repair the source."""
    checks = {}
    with closing(_readonly(path)) as source, closing(sqlite3.connect(':memory:')) as copy:
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        deadline = time.monotonic() + 10
        def progress(status, remaining, total):
            if total * page_size > 64 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError('Diagnostic snapshot exceeds size/time limits')
        source.backup(copy, pages=128, progress=progress, sleep=0.01)
        version = copy.execute('PRAGMA user_version').fetchone()[0]
        checks['schema_version'] = version == server.SCHEMA_VERSION
        checks['sqlite_integrity'] = copy.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        checks['foreign_keys'] = not copy.execute('PRAGMA foreign_key_check').fetchall()
        tables = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checks['required_tables'] = {'memories', 'handoffs', 'chunks', 'memories_fts'} <= tables
        if checks['required_tables']:
            try:
                copy.execute("INSERT INTO memories_fts(memories_fts, rank) VALUES('integrity-check', 1)")
                checks['fts_content_matches'] = True
            except sqlite3.DatabaseError:
                checks['fts_content_matches'] = False
            checks['memories_have_chunks'] = copy.execute(
                'SELECT COUNT(*) FROM memories m WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.memory_id=m.id)'
            ).fetchone()[0] == 0
            checks['chunk_text_matches'] = all(
                [r[0] for r in copy.execute('SELECT text FROM chunks WHERE memory_id=? ORDER BY ord', (mid,))]
                == server.chunk_text(content)
                for mid, content in copy.execute('SELECT id,content FROM memories')
            )
        return {'ok': all(checks.values()), 'schema_version': version, 'checks': checks,
                'content_included': False, 'source_modified': False}


def export_project(database, output, project):
    project = server._project_key(project)
    output = safe_absolute_path(Path(output))
    with closing(_readonly(database)) as connection:
        connection.execute('BEGIN')
        if connection.execute('PRAGMA user_version').fetchone()[0] != server.SCHEMA_VERSION:
            raise ValueError('Upgrade a copy with the supported server before export')
        for table, group, fields in (('memories', 'scope', MEMORY_FIELDS), ('handoffs', 'project', HANDOFF_FIELDS)):
            size = '+'.join('COALESCE(length(CAST(' + field + ' AS BLOB)),0)' for field in sorted(fields))
            count, total = connection.execute('SELECT COUNT(*), COALESCE(SUM(' + size + '),0) FROM '
                                             + table + ' WHERE ' + group + '=?', (project,)).fetchone()
            if count > MAX_ROWS or total > MAX_BYTES:
                raise ValueError('Project exceeds transfer size/count limits')
        memories = [server.row_to_dict(r) for r in connection.execute(
            'SELECT * FROM memories WHERE scope=? ORDER BY id LIMIT ?', (project, MAX_ROWS + 1))]
        handoffs = [server.handoff_to_dict(r) for r in connection.execute(
            'SELECT * FROM handoffs WHERE project=? ORDER BY id LIMIT ?', (project, MAX_ROWS + 1))]
    value = {'format': 'unified-memory-project', 'version': 1, 'project': project,
             'memories': memories, 'handoffs': handoffs}
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise ValueError('Export exceeds byte limit')
    _validate(value)
    # Atomic publication by hard link refuses overwrite even if another writer races.
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.memory-export-', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, output)
    finally:
        Path(name).unlink(missing_ok=True)
    return {'exported': True, 'memories': len(memories), 'handoffs': len(handoffs)}


def _validate(value):
    if (not isinstance(value, dict) or set(value) != {'format', 'version', 'project', 'memories', 'handoffs'}
            or value['format'] != 'unified-memory-project' or type(value['version']) is not int or value['version'] != 1):
        raise ValueError('Unsupported transfer format')
    if not isinstance(value['project'], str) or server._project_key(value['project']) != value['project']:
        raise ValueError('Invalid project key')
    for name, fields in (('memories', MEMORY_FIELDS), ('handoffs', HANDOFF_FIELDS)):
        records = value[name]
        if not isinstance(records, list) or len(records) > MAX_ROWS:
            raise ValueError('Invalid record count')
        ids = set()
        for row in records:
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError('Invalid record fields')
            for key in ('id', 'revision'):
                if type(row[key]) is not int or not 1 <= row[key] <= 2**63 - 1:
                    raise ValueError('Invalid identity or revision')
            if row['id'] in ids:
                raise ValueError('Duplicate record ID')
            ids.add(row['id'])
            group = 'scope' if name == 'memories' else 'project'
            if row[group] != value['project']:
                raise ValueError('Cross-project content is not allowed')
            arrays = {'tags'} if name == 'memories' else set(server.HANDOFF_ARRAY_FIELDS)
            nullable = {'superseded_at'} if name == 'memories' else {'resumed_at'}
            for key in fields - {'id', 'revision', 'superseded_by'}:
                item = row[key]
                if key in arrays:
                    if not isinstance(item, list):
                        raise ValueError('Expected array')
                elif key == 'metadata':
                    if not isinstance(item, dict):
                        raise ValueError('Expected metadata object')
                elif not (isinstance(item, str) or key in nullable and item is None):
                    raise ValueError('Expected text field')
            if name == 'memories':
                if not row['content'].strip() or any(not isinstance(t, str) for t in row['tags']):
                    raise ValueError('Invalid memory content or tags')
            elif row['status'] not in server.HANDOFF_STATUSES or not row['summary'].strip():
                raise ValueError('Invalid handoff status or summary')
            elif row['task_id']:
                server._selector(row, 'task_id')
    memories = {row['id']: row for row in value['memories']}
    for row in memories.values():
        target = row['superseded_by']
        if target is not None and (type(target) is not int or not 1 <= target <= 2**63 - 1):
            raise ValueError('Invalid replacement ID')
    visited = set()
    for mid in memories:
        chain = set()
        while mid in memories and mid not in visited:
            if mid in chain:
                raise ValueError('Replacement cycle')
            chain.add(mid)
            mid = memories[mid]['superseded_by']
        visited.update(chain)


def load_export(path):
    path = safe_absolute_path(Path(path))
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Export exceeds byte limit')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def nonfinite(_):
        raise ValueError('Nonfinite JSON is not supported')
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    _validate(value)
    return value


def import_project(path, database, *, confirm=False):
    value = load_export(path)
    result = {'imported': False, 'project': value['project'], 'memories': len(value['memories']),
              'handoffs': len(value['handoffs']), 'content_included': False}
    if confirm is not True:
        return {**result, 'mode': 'preview'}
    destination = safe_absolute_path(Path(database))
    if destination.exists():
        raise ValueError('Import requires a new database; existing stores are never merged or overwritten')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.memory-import-', suffix='.db', dir=destination.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(name)) as connection:
            server._init_schema(connection)
            connection.execute('PRAGMA foreign_keys=ON')
            with connection:
                for table in ('memories', 'handoffs'):
                    for row in value[table]:
                        converted = {k: json.dumps(v, allow_nan=False) if isinstance(v, (list, dict)) else v
                                     for k, v in row.items()}
                        columns = sorted(converted)
                        connection.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES('
                                           + ','.join('?' for _ in columns) + ')', [converted[k] for k in columns])
                for row in value['memories']:
                    connection.executemany('INSERT INTO chunks(memory_id,ord,text) VALUES(?,?,?)',
                        [(row['id'], number, text) for number, text in enumerate(server.chunk_text(row['content']))])
        if not doctor(name)['ok']:
            raise ValueError('Imported database failed integrity checks')
        os.link(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
    return {**result, 'imported': True, 'mode': 'new_database'}
