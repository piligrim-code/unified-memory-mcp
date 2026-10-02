"""Official MCP SDK interoperability; optional test dependency, synthetic stores."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from importlib.metadata import version
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

import server


SDK_MAJOR = int(version('mcp').split('.')[0])


@asynccontextmanager
async def client(database):
    from mcp import StdioServerParameters
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('UNIFIED_MEMORY_', 'MEMORY_', 'CLAUDE_MEM', 'OPENAI_', 'ANTHROPIC_'))}
    env.update(UNIFIED_MEMORY_DB=str(database), MEMORY_AUTOSYNC='0', MEMORY_ENABLE_EMBEDDINGS='0')
    params = StdioServerParameters(command=sys.executable, args=[server.__file__], env=env)
    with open(os.devnull, 'w') as errors:
        if SDK_MAJOR == 1:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session:
                    initialized = await session.initialize()
                    yield session, initialized.model_dump(by_alias=True)
        elif SDK_MAJOR == 2:
            from mcp import Client
            async with Client(params, read_timeout_seconds=10) as session:
                yield session, session.session.initialize_result.model_dump(by_alias=True)
        else:
            raise RuntimeError('Unqualified SDK major version')


def wire(result):
    return result.model_dump(by_alias=True)


async def call(session, name, **arguments):
    response = wire(await session.call_tool(name, arguments))
    if response.get('isError'):
        raise AssertionError('Unexpected tool error: ' + name)
    return json.loads(response['content'][0]['text'])


class SDKClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Path(self.tmp.name) / 'synthetic.db'
        self.socket_guard = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('No outbound network allowed'))
        self.socket_guard.start()

    async def asyncTearDown(self):
        self.socket_guard.stop()
        self.tmp.cleanup()

    async def test_negotiation_does_not_claim_unimplemented_protocol(self):
        async with client(self.database) as (session, initialized):
            self.assertEqual(server.DEFAULT_PROTOCOL, initialized['protocolVersion'])
            self.assertEqual(server.SERVER_VERSION, initialized['serverInfo']['version'])
            self.assertIn('instructions', initialized)

    async def test_catalog_parses_in_sdk_and_schemas_are_valid(self):
        from jsonschema import Draft202012Validator
        async with client(self.database) as (session, _):
            catalog = wire(await session.list_tools())['tools']
            names = {tool['name'] for tool in catalog}
            self.assertIn('memory_supersede', names)
            self.assertIn('handoff_save', names)
            self.assertEqual(len(names), len(catalog))
            for tool in catalog:
                with self.subTest(tool=tool['name']):
                    Draft202012Validator.check_schema(tool['inputSchema'])

    async def test_two_live_clients_have_one_revision_winner(self):
        async with client(self.database) as (a, _), client(self.database) as (b, _):
            saved = await call(a, 'handoff_save', project='sample', task_id='ticket',
                               source='author', summary='Synthetic checkpoint')
            row = saved['handoff']
            outputs = await asyncio.gather(
                call(a, 'handoff_save', project='sample', id=row['id'], source='author',
                     summary='First candidate', expected_revision=1),
                call(b, 'handoff_save', project='sample', id=row['id'], source='author',
                     summary='Second candidate', expected_revision=1),
            )
            self.assertEqual(1, sum(r['saved'] for r in outputs))
            self.assertEqual(1, sum(r.get('reason') == 'conflict' for r in outputs))
            resumed = await call(b, 'memory_bootstrap', project='sample', task_id='ticket',
                                 consumer='reader', memory_limit=0)
            self.assertEqual(2, resumed['handoff']['handoffs'][0]['revision'])

    async def test_failures_are_tool_results_not_broken_sessions(self):
        async with client(self.database) as (session, _):
            failed = wire(await session.call_tool('handoff_save', {'project': 'sample', 'summary': 'No source'}))
            self.assertTrue(failed['isError'])
            result = await call(session, 'handoff_save', project='sample', source='author', summary='Valid')
            self.assertTrue(result['saved'])

    async def test_restart_and_supersession(self):
        async with client(self.database) as (session, _):
            old = await call(session, 'memory_save', scope='sample', source='fixture', content='Synthetic old')
            new = await call(session, 'memory_save', scope='sample', source='fixture', content='Synthetic new')
            await call(session, 'memory_supersede', id=old['id'], replacement_id=new['id'],
                       expected_revision=1, replacement_revision=1)
        async with client(self.database) as (session, _):
            found = await call(session, 'memory_recall', query='Synthetic', scope='sample')
            self.assertEqual([new['id']], [r['memory_id'] for r in found['results']])
            historical = await call(session, 'memory_get', id=old['id'])
            self.assertEqual(new['id'], historical['memory']['superseded_by'])

    async def test_empty_disabled_and_wrong_task_responses(self):
        async with client(self.database) as (session, _):
            await call(session, 'handoff_save', project='sample', task_id='one', source='author', summary='Task one')
            for arguments, reason in (({'task_id': 'two'}, 'no_match'), ({'handoff_limit': 0}, 'disabled')):
                with self.subTest(arguments=arguments):
                    result = await call(session, 'memory_bootstrap', project='sample', consumer='reader',
                                        memory_limit=0, **arguments)
                    self.assertEqual((0, reason), (result['handoff']['count'], result['handoff']['reason']))


if __name__ == '__main__':
    print('Official MCP SDK:', version('mcp'), flush=True)
    unittest.main()
