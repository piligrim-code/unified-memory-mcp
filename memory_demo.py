"""Two disposable stdio clients exchanging one synthetic task checkpoint."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import server


def call(database, name, arguments):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('UNIFIED_MEMORY_', 'MEMORY_', 'CLAUDE_MEM'))}
    env.update(UNIFIED_MEMORY_DB=str(database), MEMORY_AUTOSYNC='0', MEMORY_ENABLE_EMBEDDINGS='0')
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': server.DEFAULT_PROTOCOL,
                    'capabilities': {}, 'clientInfo': {'name': 'synthetic-demo', 'version': '1'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
         'params': {'name': name, 'arguments': arguments}},
    ]
    run = subprocess.run([sys.executable, server.__file__],
                         input=''.join(json.dumps(r) + '\n' for r in requests),
                         capture_output=True, text=True, env=env, timeout=20)
    if run.returncode:
        raise RuntimeError('Synthetic client failed; no transcript emitted')
    responses = [json.loads(line) for line in run.stdout.splitlines()]
    response = next(r for r in responses if r.get('id') == 2)
    if 'error' in response or response['result'].get('isError'):
        raise RuntimeError('Synthetic tool call failed; no transcript emitted')
    return json.loads(response['result']['content'][0]['text'])


def demo(work_root):
    root = Path(work_root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    database = root / 'synthetic.db'
    saved = call(database, 'handoff_save', dict(project='demo', task_id='ticket-1', source='client-a',
                 target='client-b', summary='Synthetic checkpoint: parser tests are pending.'))['handoff']
    discovered = call(database, 'memory_bootstrap', dict(project='demo', task_id='ticket-1',
                       consumer='client-b', memory_limit=0))['handoff']['handoffs'][0]
    if discovered['id'] != saved['id'] or discovered['resumed_by']:
        raise AssertionError('Read-only task discovery failed')
    resumed = call(database, 'handoff_load', dict(project='demo', consumer='client-b', id=saved['id'],
                   mark_resumed=True, expected_revision=discovered['revision']))['handoffs'][0]
    stale = call(database, 'handoff_save', dict(project='demo', source='client-a', id=saved['id'],
                 expected_revision=saved['revision'], summary='Stale synthetic write'))
    if stale.get('reason') != 'conflict':
        raise AssertionError('Stale write was not rejected')
    complete = call(database, 'handoff_complete', dict(id=saved['id'], expected_revision=resumed['revision']))
    if not complete['completed']:
        raise AssertionError('Completion failed')
    return {'ok': True, 'clients': 2, 'processes': 5, 'task_selected': True,
            'stale_write_rejected': True, 'content_included': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', required=True)
    args = parser.parse_args()
    print(json.dumps(demo(args.work_root)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
