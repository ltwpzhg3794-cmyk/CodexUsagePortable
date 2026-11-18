"""Local, read-only Codex usage dashboard. Python standard library only."""
import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from throughput import Throughput, number

BASE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
          'output_tokens', 'reasoning_output_tokens', 'total_tokens')
TAIL_BYTES = 8 * 1024 * 1024
OUTPUT_RECORD = re.compile(
    rb'"type"\s*:\s*"response_item"\s*,\s*"payload"\s*:\s*\{\s*'
    rb'"type"\s*:\s*"(?:message|reasoning|function_call|custom_tool_call)"')


def usage(value):
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) if isinstance(value.get(key), (int, float)) else None
            for key in FIELDS}


def parse_tail(raw, truncated=False):
    """Prefer authoritative thread totals; never sum repeated cumulative events."""
    lines = raw.splitlines()
    if truncated:
        lines = lines[1:]
    records, fallback = [], []
    output_items, item_starts = [], {}
    response_boundary = not truncated
    seen = set()
    for line in lines:
        # Skip tool results; only output-item metadata is needed for TPS.
        if (b'"token_usage_record"' not in line and b'"token_count"' not in line
                and b'"item_completed"' not in line and b'"task_started"' not in line
                and not OUTPUT_RECORD.search(line)):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        p = obj.get('payload') or {}
        if obj.get('type') == 'response_item':
            kind = p.get('type')
            if (kind in ('reasoning', 'function_call', 'custom_tool_call')
                    or (kind == 'message' and p.get('role') == 'assistant')):
                if p.get('id'):
                    output_items.append(p['id'])
        elif obj.get('type') == 'event_msg' and p.get('type') == 'item_completed':
            item = p.get('item') or {}
            if item.get('type') in ('AgentMessage', 'Reasoning'):
                start = p.get('started_at_ms')
                if item.get('id') and number(start):
                    item_starts[item['id']] = start
        elif obj.get('type') == 'event_msg' and p.get('type') == 'task_started':
            output_items, item_starts = [], {}
            response_boundary = True
        elif obj.get('type') == 'token_usage_record':
            rid = p.get('response_id')
            if rid and rid in seen:
                continue
            if rid:
                seen.add(rid)
            if usage(p.get('usage')):
                first_start = item_starts.get(output_items[0]) if output_items else None
                records.append({'time': obj.get('timestamp'), 'usage': usage(p['usage']),
                                'turn': usage(p.get('turn_token_usage')),
                                'total': usage(p.get('thread_token_usage')),
                                'response_id': rid,
                                'output_item_ids': output_items if response_boundary else [],
                                'stream_started_at_ms': first_start if response_boundary else None})
                output_items, item_starts = [], {}
                response_boundary = True
        elif obj.get('type') == 'event_msg' and p.get('type') == 'token_count':
            info = p.get('info') or {}
            total = usage(info.get('total_token_usage'))
            last = usage(info.get('last_token_usage'))
            if total and last and (not fallback or fallback[-1]['total'] != total):
                fallback.append({'time': obj.get('timestamp'), 'usage': last,
                                 'total': total, 'turn': None})
    selected = records if records else fallback
    if not selected:
        return {'available': False, 'recent': [], 'truncated': truncated,
                'tps': None, 'tps_detail': None}
    latest = selected[-1]
    return {'available': True, 'total': latest['total'], 'last': latest['usage'],
            'turn': latest['turn'], 'recorded_at': latest['time'],
            'recent': [{'time': r['time'], 'usage': r['usage']} for r in selected[-30:]][::-1],
            'source': 'thread' if records else 'session', 'truncated': truncated,
            'last_response': {'time': latest['time'],
                              'response_id': latest.get('response_id'),
                              'output_tokens': latest['usage'].get('output_tokens'),
                              'output_item_ids': latest.get('output_item_ids', []),
                              'stream_started_at_ms': latest.get('stream_started_at_ms')},
            'tps': None, 'tps_detail': None}


class Store:
    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.index_at = 0
        self.items = []
        self.paths = {}
        self.cache = {}
        self.throughput = Throughput(root.resolve())

    def catalog(self):
        if time.monotonic() - self.index_at < 4:
            return self.items
        names = {}
        try:
            with (self.root / 'session_index.jsonl').open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        names[row['id']] = row.get('thread_name') or row['id']
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
        items, paths = [], {}
        for folder in ('sessions', 'archived_sessions'):
            for path in (self.root / folder).rglob('*.jsonl'):
                match = re.search(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$', path.name)
                if not match:
                    continue
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                sid = match[1]
                if sid in paths:
                    continue
                paths[sid] = path
                items.append({'id': sid, 'title': names.get(sid, '未命名任务 · ' + sid[:8]),
                              'modified': modified, 'archived': folder == 'archived_sessions'})
        self.items = sorted(items, key=lambda x: x['modified'], reverse=True)
        self.paths = paths
        self.index_at = time.monotonic()
        return self.items

    def detail(self, sid):
        self.catalog()
        path = self.paths.get(sid)
        if not path:
            raise KeyError(sid)
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        old = self.cache.get(sid)
        if old and old[0] == signature:
            # SQLite logging can finish just after the usage file is written.
            if old[1].get('last_response') and old[1].get('tps') is None:
                speed = self.throughput.read(sid, old[1]['last_response'])
                if speed:
                    old[1]['tps_detail'] = speed
                    old[1]['tps'] = speed['tokens_per_second']
            return old[1]
        with path.open('rb') as stream:
            offset = max(0, stat.st_size - TAIL_BYTES)
            stream.seek(offset)
            data = parse_tail(stream.read(), offset > 0)
        # If a huge tool result follows the last usage event, keep the last known
        # counters instead of showing zero while the task is running.
        if not data['available'] and old:
            data = dict(old[1], stale=True)
        elif data['available']:
            speed = self.throughput.read(sid, data['last_response'])
            data['tps_detail'] = speed
            data['tps'] = speed['tokens_per_second'] if speed else None
        data['id'] = sid
        self.cache[sid] = signature, data
        return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18761)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    store = Store(args.root)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get('Host') not in (f'127.0.0.1:{args.port}', f'localhost:{args.port}'):
                self.send_error(403)
                return
            route = urlparse(self.path)
            try:
                if route.path == '/':
                    body, kind = (BASE / 'dist/index.html').read_bytes(), 'text/html; charset=utf-8'
                elif route.path == '/api/health':
                    body, kind = b'{"app":"codex-local-usage"}', 'application/json'
                elif route.path == '/api/tasks':
                    with store.lock:
                        data = {'tasks': store.catalog(), 'root_exists': args.root.is_dir()}
                    body, kind = json.dumps(data, ensure_ascii=False).encode(), 'application/json; charset=utf-8'
                elif route.path == '/api/usage':
                    sid = parse_qs(route.query).get('id', [''])[0]
                    with store.lock:
                        data = store.detail(sid)
                    body, kind = json.dumps(data, ensure_ascii=False).encode(), 'application/json; charset=utf-8'
                else:
                    self.send_error(404)
                    return
            except KeyError:
                self.send_error(404, 'Task not found')
                return
            except OSError:
                self.send_error(503, 'Local records temporarily unavailable')
                return
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Codex usage: http://127.0.0.1:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
