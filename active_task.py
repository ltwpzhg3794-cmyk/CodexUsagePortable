"""Read only explicit view/navigation events, never infer from model activity."""
import os
import re
import time
from pathlib import Path

UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
FIELDS = re.compile(r'(\w+)=([^\s]+)')


class ActiveTask:
    def __init__(self, root=None):
        self.root = Path(root or Path(os.environ['LOCALAPPDATA']) / 'Codex/Logs')
        self.pid = None
        self.paths = []
        self.offsets = {}
        self.pending = {}
        self.views = {}
        self.discovered = 0
        self.last_event = None

    def consume(self, line):
        if 'thread_stream_view_activity_changed' not in line and 'received browser sidebar owner sync' not in line:
            return
        fields = dict(FIELDS.findall(line))
        timestamp = line[:24]
        if 'thread_stream_view_activity_changed' in line:
            if fields.get('rendererWindowAppearance') != 'primary':
                return
            window = fields.get('rendererWindowId')
            sid = fields.get('conversationId', '')
            if not window:
                return
            old = self.views.get(window)
            if old and timestamp < old['time']:
                return
            if fields.get('active') == 'true':
                self.views[window] = {'id': sid if UUID.fullmatch(sid) else None, 'time': timestamp}
            elif old and old['id'] == sid:
                self.views[window] = {'id': None, 'time': timestamp}
        else:
            window = fields.get('windowId')
            route = fields.get('ownerRoutePath', '')
            if not window or not route:
                return
            old = self.views.get(window)
            if old and timestamp < old['time']:
                return
            # /local/<id> is the actual foreground route, not conversationId,
            # which can temporarily be a client-new-thread placeholder.
            match = re.fullmatch(r'/local/([0-9a-f-]{36})(?:[/?].*)?', route)
            sid = match[1] if match and UUID.fullmatch(match[1]) else None
            self.views[window] = {'id': sid, 'time': timestamp}
        self.last_event = timestamp

    def poll(self, pid):
        if self.pid != pid:
            self.pid = pid
            self.paths = []
            self.offsets.clear()
            self.pending.clear()
            self.views.clear()
            self.discovered = 0
        if time.monotonic() - self.discovered > 2:
            # The native main process ID is included in each desktop log name.
            self.paths = sorted(self.root.glob(f'*/*/*/codex-desktop-*-{pid}-t0-*.log'),
                                key=lambda p: p.stat().st_mtime)[-6:]
            self.discovered = time.monotonic()
        for path in self.paths:
            try:
                size = path.stat().st_size
                previous = self.offsets.get(path)
                if previous is not None and size == previous:
                    continue
                initial = previous is None or size < previous
                start = max(0, size - 4 * 1024 * 1024) if initial else previous
                with path.open('rb') as stream:
                    stream.seek(start)
                    data = stream.read()
                    self.offsets[path] = stream.tell()
                if initial and start:
                    data = data.partition(b'\n')[2]
                data = self.pending.get(path, b'') + data
                lines = data.split(b'\n')
                self.pending[path] = lines.pop()
                for line in lines:
                    if b'thread_stream_view_activity_changed' in line or b'received browser sidebar owner sync' in line:
                        self.consume(line.decode('utf-8', 'replace'))
            except OSError:
                continue
        # Multiple native main windows do not have a public HWND-to-renderer-ID
        # mapping. The caller refuses to guess when it detects that situation.
        return max(self.views.values(), key=lambda v: v['time'])['id'] if self.views else None
