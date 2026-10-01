"""Authenticated Streamable HTTP MCP gateway for remote Claude clients.

The gateway exposes the unified memory tools and a deliberately small set of
text-file operations rooted at one workspace. It binds to loopback by default;
use an SSH reverse tunnel instead of exposing this port to the public network.
"""
import argparse
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import server as memory
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
WORKSPACE_ROOT = os.path.realpath(os.environ.get('MEMORY_WORKSPACE_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def configure_workspace(path):
    global WORKSPACE_ROOT
    root = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(root):
        raise ValueError('workspace root is not a directory: %s' % root)
    WORKSPACE_ROOT = root
    return root

def resolve_workspace_path(value, must_exist=False):
    raw = str(value or '.').strip()
    candidate = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(WORKSPACE_ROOT, raw))
    try:
        inside = os.path.commonpath([os.path.normcase(WORKSPACE_ROOT), os.path.normcase(candidate)]) == os.path.normcase(WORKSPACE_ROOT)
    except ValueError:
        inside = False
    if not inside:
        raise ValueError('path escapes the configured workspace')
    if must_exist and (not os.path.exists(candidate)):
        raise ValueError('path does not exist: %s' % raw)
    return candidate

def relative_path(path):
    rel = os.path.relpath(path, WORKSPACE_ROOT)
    return '.' if rel == '.' else rel.replace('\\', '/')

def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()

def _read_text(path):
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise ValueError('file exceeds %d bytes' % MAX_FILE_BYTES)
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

def _atomic_write(path, content):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.memory-write-', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def tool_workspace_stat(args):
    path = resolve_workspace_path(args.get('path'), must_exist=True)
    result = {'path': relative_path(path), 'type': 'directory' if os.path.isdir(path) else 'file', 'size': os.path.getsize(path), 'modified': os.path.getmtime(path)}
    if os.path.isfile(path):
        result['sha256'] = file_sha256(path)
    return result

def tool_workspace_list(args):
    root = resolve_workspace_path(args.get('path'), must_exist=True)
    if not os.path.isdir(root):
        raise ValueError('path is not a directory')
    depth = max(0, min(int(args.get('depth') or 1), 5))
    limit = max(1, min(int(args.get('limit') or 500), 2000))
    include_hidden = bool(args.get('include_hidden', False))
    results = []

    def walk(directory, level):
        if len(results) >= limit:
            return
        entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.casefold()))
        for entry in entries:
            if len(results) >= limit:
                return
            if not include_hidden and entry.name.startswith('.'):
                continue
            try:
                safe_path = resolve_workspace_path(entry.path)
            except ValueError:
                continue
            item = {'path': relative_path(safe_path), 'type': 'directory' if entry.is_dir(follow_symlinks=False) else 'file'}
            if item['type'] == 'file':
                item['size'] = entry.stat(follow_symlinks=False).st_size
            results.append(item)
            if item['type'] == 'directory' and level < depth:
                walk(safe_path, level + 1)
    walk(root, 1)
    return {'root': relative_path(root), 'count': len(results), 'truncated': len(results) >= limit, 'entries': results}

def tool_workspace_read(args):
    path = resolve_workspace_path(args.get('path'), must_exist=True)
    if not os.path.isfile(path):
        raise ValueError('path is not a file')
    text = _read_text(path)
    lines = text.splitlines(keepends=True)
    start = max(1, int(args.get('start_line') or 1))
    end = min(len(lines), int(args.get('end_line') or len(lines)))
    selected = '' if end < start else ''.join(lines[start - 1:end])
    return {'path': relative_path(path), 'sha256': file_sha256(path), 'size': os.path.getsize(path), 'total_lines': len(lines), 'start_line': start, 'end_line': end, 'content': selected}

def tool_workspace_write(args):
    path = resolve_workspace_path(args.get('path'))
    content = args.get('content')
    if not isinstance(content, str):
        raise ValueError('`content` must be a string')
    if len(content.encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('content exceeds %d bytes' % MAX_FILE_BYTES)
    existed = os.path.exists(path)
    if existed:
        if not os.path.isfile(path):
            raise ValueError('target exists and is not a file')
        expected = str(args.get('expected_sha256') or '').strip().casefold()
        if not expected:
            raise ValueError('`expected_sha256` is required when overwriting a file')
        actual = file_sha256(path)
        if not hmac.compare_digest(expected, actual):
            raise ValueError('file changed: expected sha256 does not match')
    _atomic_write(path, content)
    return {'written': True, 'created': not existed, 'path': relative_path(path), 'sha256': file_sha256(path), 'size': os.path.getsize(path)}

def tool_workspace_replace(args):
    path = resolve_workspace_path(args.get('path'), must_exist=True)
    if not os.path.isfile(path):
        raise ValueError('path is not a file')
    old = args.get('old')
    new = args.get('new')
    if not isinstance(old, str) or not old:
        raise ValueError('`old` must be a non-empty string')
    if not isinstance(new, str):
        raise ValueError('`new` must be a string')
    text = _read_text(path)
    count = text.count(old)
    expected_count = int(args.get('expected_count') or 1)
    if count != expected_count:
        raise ValueError('expected %d matches, found %d' % (expected_count, count))
    expected_sha = str(args.get('expected_sha256') or '').strip().casefold()
    actual_sha = file_sha256(path)
    if expected_sha and (not hmac.compare_digest(expected_sha, actual_sha)):
        raise ValueError('file changed: expected sha256 does not match')
    updated = text.replace(old, new, expected_count)
    if len(updated.encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('updated content exceeds %d bytes' % MAX_FILE_BYTES)
    _atomic_write(path, updated)
    return {'replaced': True, 'replacements': expected_count, 'path': relative_path(path), 'sha256': file_sha256(path)}

def tool_workspace_mkdir(args):
    path = resolve_workspace_path(args.get('path'))
    os.makedirs(path, exist_ok=True)
    return {'created': True, 'path': relative_path(path)}

def tool_workspace_delete(args):
    path = resolve_workspace_path(args.get('path'), must_exist=True)
    if not os.path.isfile(path):
        raise ValueError('only files can be deleted through this gateway')
    expected = str(args.get('expected_sha256') or '').strip().casefold()
    if not expected:
        raise ValueError('`expected_sha256` is required')
    actual = file_sha256(path)
    if not hmac.compare_digest(expected, actual):
        raise ValueError('file changed: expected sha256 does not match')
    os.unlink(path)
    return {'deleted': True, 'path': relative_path(path)}

def _git(args, diff=False):
    cwd = resolve_workspace_path(args.get('path'), must_exist=True)
    if not os.path.isdir(cwd):
        cwd = os.path.dirname(cwd)
    command = ['git', 'diff', '--no-ext-diff'] if diff else ['git', 'status', '--short', '--branch']
    if diff and bool(args.get('staged', False)):
        command.append('--cached')
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    output = proc.stdout
    max_chars = max(1000, min(int(args.get('max_chars') or 100000), 500000))
    return {'cwd': relative_path(cwd), 'exit_code': proc.returncode, 'truncated': len(output) > max_chars, 'output': output[:max_chars], 'stderr': proc.stderr[:10000]}

def tool_workspace_git_status(args):
    return _git(args, diff=False)

def tool_workspace_git_diff(args):
    return _git(args, diff=True)
WORKSPACE_TOOLS = [{'name': 'workspace_stat', 'description': 'Stat a local workspace path.', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}, {'name': 'workspace_list', 'description': 'List files under the guarded local workspace.', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'depth': {'type': 'integer'}, 'limit': {'type': 'integer'}, 'include_hidden': {'type': 'boolean'}}}}, {'name': 'workspace_read', 'description': 'Read a UTF-8 file and return its SHA-256 revision.', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'start_line': {'type': 'integer'}, 'end_line': {'type': 'integer'}}, 'required': ['path']}}, {'name': 'workspace_write', 'description': 'Create or atomically overwrite a UTF-8 file. Overwrites require the SHA from workspace_read.', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}, 'expected_sha256': {'type': 'string'}}, 'required': ['path', 'content']}}, {'name': 'workspace_replace', 'description': 'Atomically replace an exact text fragment in a UTF-8 file.', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'old': {'type': 'string'}, 'new': {'type': 'string'}, 'expected_count': {'type': 'integer'}, 'expected_sha256': {'type': 'string'}}, 'required': ['path', 'old', 'new']}}, {'name': 'workspace_mkdir', 'description': 'Create a directory inside the guarded workspace.', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}, {'name': 'workspace_delete', 'description': 'Delete one file after verifying its SHA-256. Directories cannot be deleted.', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'expected_sha256': {'type': 'string'}}, 'required': ['path', 'expected_sha256']}}, {'name': 'workspace_git_status', 'description': 'Run read-only git status in a local workspace directory.', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}, {'name': 'workspace_git_diff', 'description': 'Run read-only git diff in a local workspace directory.', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'staged': {'type': 'boolean'}, 'max_chars': {'type': 'integer'}}}}]
WORKSPACE_DISPATCH = {'workspace_stat': tool_workspace_stat, 'workspace_list': tool_workspace_list, 'workspace_read': tool_workspace_read, 'workspace_write': tool_workspace_write, 'workspace_replace': tool_workspace_replace, 'workspace_mkdir': tool_workspace_mkdir, 'workspace_delete': tool_workspace_delete, 'workspace_git_status': tool_workspace_git_status, 'workspace_git_diff': tool_workspace_git_diff}

def gateway_token():
    value = os.environ.get('MEMORY_GATEWAY_TOKEN', '').strip()
    token_file = os.environ.get('MEMORY_GATEWAY_TOKEN_FILE', '').strip()
    if not value and token_file:
        with open(os.path.expanduser(token_file), 'r', encoding='utf-8') as f:
            value = f.read().strip()
    if len(value) < 32:
        raise RuntimeError('MEMORY_GATEWAY_TOKEN or a token file with at least 32 characters is required')
    return value

def tool_result(name, args):
    try:
        if name in memory.DISPATCH:
            result = memory.DISPATCH[name](args)
        elif name in WORKSPACE_DISPATCH:
            result = WORKSPACE_DISPATCH[name](args)
        else:
            raise ValueError('unknown tool: %s' % name)
        return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False, indent=2)}], 'isError': False}
    except Exception as e:
        return {'content': [{'type': 'text', 'text': 'Error in %s: %s' % (name, e)}], 'isError': True}

def dispatch(message):
    method = message.get('method')
    request_id = message.get('id')
    params = message.get('params') or {}
    if 'id' not in message:
        return None
    if method == 'initialize':
        result = memory.handle_initialize(params)
        result['serverInfo'] = {'name': 'unified-memory-gateway', 'version': memory.SERVER_VERSION}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': memory.TOOLS + WORKSPACE_TOOLS}
    elif method == 'tools/call':
        result = tool_result(params.get('name'), params.get('arguments') or {})
    elif method in ('resources/list', 'resources/templates/list'):
        result = {'resources': [], 'resourceTemplates': []}
    elif method == 'prompts/list':
        result = {'prompts': []}
    else:
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': 'Method not found: %s' % method}}
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}

class GatewayHandler(BaseHTTPRequestHandler):
    server_version = 'UnifiedMemoryGateway/2.3'

    def log_message(self, fmt, *args):
        memory.log('http', self.address_string(), fmt % args)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        supplied = self.headers.get('Authorization', '')
        expected = 'Bearer ' + self.server.gateway_token
        return hmac.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8'))

    def do_GET(self):
        if self.path == '/health':
            self._json(200, {'ok': True, 'server': 'unified-memory-gateway', 'workspace': WORKSPACE_ROOT})
            return
        self._json(405, {'error': 'Use POST /mcp'})

    def do_POST(self):
        if self.path != '/mcp':
            self._json(404, {'error': 'not found'})
            return
        if not self._authorized():
            self._json(401, {'error': 'unauthorized'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError('invalid request size')
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
            if isinstance(payload, list):
                responses = [r for r in (dispatch(item) for item in payload) if r is not None]
                if not responses:
                    self.send_response(202)
                    self.end_headers()
                else:
                    self._json(200, responses)
            else:
                response = dispatch(payload)
                if response is None:
                    self.send_response(202)
                    self.end_headers()
                else:
                    self._json(200, response)
        except Exception as e:
            self._json(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': str(e)}})
        finally:
            memory.close_thread_connection()

class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=os.environ.get('MEMORY_GATEWAY_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('MEMORY_GATEWAY_PORT', '18765')))
    parser.add_argument('--workspace-root', required=True)
    args = parser.parse_args()
    configure_workspace(args.workspace_root)
    token = gateway_token()
    memory.conn()
    if os.environ.get('MEMORY_AUTOSYNC', '0') != '0':
        memory.autosync()
    httpd = GatewayServer((args.host, args.port), GatewayHandler)
    httpd.gateway_token = token
    memory.log('gateway listening on http://%s:%d/mcp workspace=%s' % (args.host, args.port, WORKSPACE_ROOT))
    httpd.serve_forever()
if __name__ == '__main__':
    main()
