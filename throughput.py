"""Estimate completed-response throughput from local output-item timings."""
from datetime import datetime
from contextlib import closing
import math
import re
import sqlite3


OUTPUT_ITEM = re.compile(r'Output item item_type="[^"]+" item_id="([^"]+)"')


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def calculate(output_tokens, start, end, source):
    if not all(number(v) for v in (output_tokens, start, end)):
        return None
    if output_tokens < 0 or end <= start:
        return None
    seconds = end - start
    return {'tokens_per_second': output_tokens / seconds,
            'output_tokens': output_tokens, 'duration_seconds': seconds,
            'source': source, 'estimated': True}


class Throughput:
    def __init__(self, root):
        self.database = root / 'logs_2.sqlite'

    def read(self, sid, record):
        end = timestamp(record.get('time'))
        ids = record.get('output_item_ids') or []
        if end is None or not ids:
            return None
        starts = []
        source = 'session_item_timing'
        start_ms = record.get('stream_started_at_ms')
        if number(start_ms):
            starts.append(start_ms / 1000)
        # Correlate actual item IDs, not nearby activity or another thread.
        # The read-only, indexed query also covers tool-call-only responses,
        # whose generation start is absent from session item-completed events.
        try:
            with closing(sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True,
                                        timeout=.1)) as connection:
                rows = connection.execute(
                    "SELECT ts, ts_nanos, feedback_log_body FROM logs "
                    "WHERE thread_id=? AND ts>=? AND ts<=? "
                    "AND target='codex_core::stream_events_utils' "
                    "ORDER BY ts DESC, ts_nanos DESC, id DESC LIMIT 2048",
                    (sid, int(end) - 3600, int(end)))
                for seconds, nanos, body in rows:
                    body = body or ''
                    if 'from="output_item_done"' in body:
                        continue
                    match = OUTPUT_ITEM.search(body)
                    if match and match[1] == ids[0]:
                        start = seconds + nanos / 1_000_000_000
                        if start < end:
                            starts.append(start)
                            source = 'local_output_item_log'
        except (sqlite3.Error, OSError, ValueError):
            pass
        if not starts:
            return None
        result = calculate(record.get('output_tokens'), min(starts), end, source)
        if result:
            result['recorded_at'] = record.get('time')
            result['response_id'] = record.get('response_id')
        return result
