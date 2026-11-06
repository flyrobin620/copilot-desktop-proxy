"""Local request counters. Never store messages, headers, user IDs or credentials."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

TOKEN_KEYS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')


class UsageObserver:
    def __init__(self):
        self.tokens = {key: 0 for key in TOKEN_KEYS}
        self.known = False
        self.complete = False
        self.failed = False
        self.buffer = b''
        self.dropping = False
        self.event_data = bytearray()
        self.event_dropping = False
        self.skip_lf = False

    def update(self, usage):
        if not isinstance(usage, dict):
            return
        for key in TOKEN_KEYS:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**53:
                self.tokens[key] = max(self.tokens[key], value)
                self.known = True

    def event(self, event):
        if not isinstance(event, dict):
            return
        if event.get('type') == 'message_start':
            self.update(event.get('message', {}).get('usage'))
        self.update(event.get('usage'))
        if event.get('type') in ('message', 'message_stop'):
            self.complete = True
        if event.get('type') == 'error':
            self.failed = True

    def _finish_line(self):
        if self.dropping:
            self.event_data.clear()
            self.event_dropping = True
        elif not self.buffer:
            # SSE dispatches at the blank line, not at the end of a data line.
            # A JSON value can span several data lines joined by newlines.
            if self.event_data and not self.event_dropping:
                try:
                    self.event(json.loads(self.event_data))
                except (ValueError, AttributeError):
                    pass
            self.event_data.clear()
            self.event_dropping = False
        elif not self.event_dropping and self.buffer.startswith(b'data:'):
            data = self.buffer[5:].removeprefix(b' ')
            if len(self.event_data) + len(data) + 1 <= 256 * 1024:
                self.event_data.extend(data + b'\n')
            else:
                self.event_data.clear()
                self.event_dropping = True
        self.buffer, self.dropping = b'', False

    def feed(self, block):
        # Observe a bounded copy; the relay still forwards original bytes unchanged.
        # CR, LF and CRLF are SSE line endings, including CRLF split across reads.
        if not block:
            return
        if self.skip_lf and block.startswith(b'\n'):
            block = block[1:]
        self.skip_lf = block.endswith(b'\r')
        for index, part in enumerate(block.replace(b'\r\n', b'\n').replace(b'\r', b'\n').split(b'\n')):
            if index:
                self._finish_line()
            if not self.dropping:
                if len(self.buffer) + len(part) <= 256 * 1024:
                    self.buffer += part
                else:
                    self.buffer, self.dropping = b'', True


class UsageStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, model TEXT NOT NULL,
                protocol TEXT NOT NULL, streaming INTEGER NOT NULL, status INTEGER NOT NULL,
                outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL, usage_known INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                cache_read_input_tokens INTEGER NOT NULL, cache_creation_input_tokens INTEGER NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS requests_time ON requests(timestamp)')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def record(self, record):
        columns = ('id', 'timestamp', 'model', 'protocol', 'streaming', 'status', 'outcome',
                   'duration_ms', 'usage_known', *TOKEN_KEYS)
        with closing(self.connect()) as db, db:
            db.execute(f"INSERT INTO requests ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                       [record[key] for key in columns])

    def report(self, period='month', model='', page=1, offset=0, now=None):
        now = now or datetime.now(timezone.utc)
        local_zone = timezone(timedelta(minutes=-offset))
        local_now = now.astimezone(local_zone)
        days = {'day': 1, 'week': 7, 'month': 30}[period]
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        iso = lambda value: value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        lower, upper = iso(start), iso(now)
        where, parameters = 'timestamp >= ? AND timestamp <= ?', [lower, upper]
        with closing(self.connect()) as db:
            all_models = [row[0] for row in db.execute(
                'SELECT DISTINCT model FROM requests WHERE ' + where + ' ORDER BY model', parameters)]
            if model:
                where += ' AND model = ?'
                parameters.append(model)
            sums = ','.join(f'COALESCE(SUM({key}),0) AS {key}' for key in TOKEN_KEYS)
            aggregate = ('COUNT(*) AS requests, COALESCE(SUM(outcome != \'success\'),0) AS errors, '
                         'COALESCE(SUM(usage_known = 0),0) AS unknown_usage, '
                         'COALESCE(AVG(duration_ms),0) AS average_duration_ms, ' + sums)
            totals = dict(db.execute(f'SELECT {aggregate} FROM requests WHERE {where}', parameters).fetchone())
            models = [dict(row) for row in db.execute(
                f'SELECT model,{aggregate} FROM requests WHERE {where} GROUP BY model ORDER BY COUNT(*) DESC', parameters)]
            modifier = f'{-offset:+d} minutes'
            date_format = '%Y-%m-%dT%H:00' if period == 'day' else '%Y-%m-%d'
            rows = db.execute(f"SELECT strftime(?,timestamp,?) AS bucket,{aggregate} FROM requests WHERE {where} GROUP BY bucket ORDER BY bucket",
                              [date_format, modifier, *parameters])
            buckets = {row['bucket']: dict(row) for row in rows}
            page_count = max(1, (totals['requests'] + 19) // 20)
            page = min(max(page, 1), page_count)
            events = [dict(row) for row in db.execute(
                f'SELECT * FROM requests WHERE {where} ORDER BY timestamp DESC,id DESC LIMIT 20 OFFSET ?',
                [*parameters, (page - 1) * 20])]
            first = db.execute('SELECT MIN(timestamp) FROM requests').fetchone()[0]
        trend = []
        for index in range(24 if period == 'day' else days):
            point = start + (timedelta(hours=index) if period == 'day' else timedelta(days=index))
            key = point.strftime('%Y-%m-%dT%H:00' if period == 'day' else '%Y-%m-%d')
            trend.append(buckets.get(key, {'bucket': key, 'requests': 0, 'errors': 0, **{k: 0 for k in TOKEN_KEYS}}))
        for row in [totals, *models, *events, *trend]:
            row['total_tokens'] = sum(row[k] for k in TOKEN_KEYS)
        return {'period': period, 'start': lower, 'end': upper, 'timezone_offset': offset,
                'recording_since': first, 'totals': totals, 'models': models, 'available_models': all_models,
                'trend': trend, 'events': events, 'page': page, 'pages': page_count,
                'cost': None, 'quota': None, 'source': 'local-proxy'}
