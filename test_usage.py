import json
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
import unittest
from usage_store import UsageObserver, UsageStore, TOKEN_KEYS


class UsageTests(unittest.TestCase):
    def test_fragmented_stream_cumulative_usage_and_cache(self):
        observer = UsageObserver()
        events = [
            {'type':'message_start','message':{'usage':{'input_tokens':4,'output_tokens':1,'cache_read_input_tokens':20,'cache_creation_input_tokens':10}}},
            {'type':'message_delta','usage':{'output_tokens':9}},
            {'type':'message_delta','usage':{'output_tokens':12}},
            {'type':'message_stop'}]
        raw = b''.join(b'data: '+json.dumps(e).encode()+b'\r\n\r\n' for e in events)
        for i in range(0,len(raw),7): observer.feed(raw[i:i+7])
        self.assertEqual(observer.tokens,dict(zip(TOKEN_KEYS,[4,12,20,10])))
        self.assertTrue(observer.known)
        self.assertTrue(observer.complete)

    def test_oversized_content_does_not_block_later_usage(self):
        observer=UsageObserver()
        observer.feed(b'data: '+b'x'*(300*1024)+b'\n\n')
        observer.feed(b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n')
        self.assertEqual(observer.tokens['output_tokens'],5)
        self.assertLess(len(observer.buffer),256*1024)
        self.assertFalse(observer.complete)

    def test_stop_is_not_complete_until_sse_blank_line(self):
        observer = UsageObserver()
        observer.feed(b'event: message_stop\ndata: {"type":"message_stop"}\n')
        self.assertFalse(observer.complete)
        observer.feed(b'')
        self.assertFalse(observer.complete)
        observer.feed(b'\n')
        self.assertTrue(observer.complete)

    def test_multiline_stop_and_all_line_endings_at_every_split(self):
        for ending in (b'\n', b'\r', b'\r\n'):
            raw = ending.join((b': comment', b'event: message_stop', b'data: {"type":',
                               b'data: "message_stop"}', b'', b''))
            for split in range(len(raw) + 1):
                with self.subTest(ending=ending, split=split):
                    observer = UsageObserver()
                    observer.feed(raw[:split])
                    observer.feed(b'')
                    observer.feed(raw[split:])
                    self.assertTrue(observer.complete)
                    self.assertFalse(observer.failed)

    def test_split_crlf_does_not_dispatch_before_blank_line(self):
        observer = UsageObserver()
        observer.feed(b'data: {"type":"message_stop"}\r')
        observer.feed(b'')
        observer.feed(b'\n')
        self.assertFalse(observer.complete)
        observer.feed(b'\r')
        self.assertTrue(observer.complete)

    def test_multiline_error_waits_for_event_boundary(self):
        observer = UsageObserver()
        observer.feed(b'data:{"type":\ndata: "error"}\n')
        self.assertFalse(observer.failed)
        observer.feed(b'\n')
        self.assertTrue(observer.failed)
        self.assertFalse(observer.complete)

    def test_oversized_multiline_event_is_bounded_and_dropped_until_blank_line(self):
        observer = UsageObserver()
        for _ in range(5):
            observer.feed(b'data: ' + b' ' * 65536 + b'\n')
            self.assertLessEqual(len(observer.event_data), 256 * 1024)
        observer.feed(b'data: {"type":"message_stop"}\n')
        self.assertFalse(observer.complete)
        observer.feed(b'\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n')
        self.assertFalse(observer.complete)
        self.assertEqual(observer.tokens['output_tokens'], 7)

    def test_oversized_line_cannot_expose_a_false_stop_in_same_event(self):
        observer = UsageObserver()
        observer.feed(b'data: ' + b'x' * (300 * 1024))
        observer.feed(b'x\ndata: {"type":"message_stop"}\n\n')
        self.assertFalse(observer.complete)
        observer.feed(b'data: {"type":"message_stop"}\n\n')
        self.assertTrue(observer.complete)

    def test_malformed_and_non_data_events_do_not_block_later_events(self):
        observer = UsageObserver()
        observer.feed(b'\n: comment\nevent: message_stop\n\ndata: invalid\n\ndata: []\n\n')
        self.assertFalse(observer.complete)
        observer.feed(b'data: {"type":"message_stop"}\n\n')
        self.assertTrue(observer.complete)

    def test_period_timezone_pagination_filter_and_persistence(self):
        with TemporaryDirectory() as temp:
            path=Path(temp)/'usage.sqlite3'
            store=UsageStore(path)
            for i in range(25):
                store.record({'id':str(i),'timestamp':'2026-10-09T18:00:00.000Z','model':'claude-opus-5',
                              'protocol':'anthropic','streaming':1,'status':200,'outcome':'success',
                              'duration_ms':100,'usage_known':1,**dict(zip(TOKEN_KEYS,[2,3,5,7]))})
            store.record({'id':'older','timestamp':'2026-10-08T18:00:00.000Z','model':'other',
                          'protocol':'openai','streaming':0,'status':400,'outcome':'error',
                          'duration_ms':10,'usage_known':0,**dict.fromkeys(TOKEN_KEYS,0)})
            now=datetime(2026,10,10,10,tzinfo=timezone.utc)
            result=UsageStore(path).report('day',page=2,offset=-480,now=now)
            self.assertEqual(result['totals']['requests'],25)
            self.assertEqual(result['totals']['total_tokens'],425)
            self.assertEqual(len(result['events']),5)
            self.assertEqual(result['trend'][2]['requests'],25)
            self.assertEqual(result['available_models'],['claude-opus-5'])
            self.assertEqual(store.report('week',model='other',now=now)['totals']['errors'],1)
            self.assertEqual(store.report('day',offset=0,now=now)['totals']['requests'],0)
            self.assertIsNone(result['cost'])
            self.assertIsNone(result['quota'])

    def test_local_proxy_history_is_isolated_and_survives_restart(self):
        with TemporaryDirectory() as temp:
            local_path=Path(temp)/'current-proxy'/'usage.sqlite3'
            other_path=Path(temp)/'other-proxy'/'usage.sqlite3'
            record={'id':'request-1','timestamp':'2026-10-10T08:00:00.000Z','model':'claude-opus-5',
                    'protocol':'anthropic','streaming':0,'status':200,'outcome':'success',
                    'duration_ms':100,'usage_known':1,**dict(zip(TOKEN_KEYS,[2,3,5,7]))}
            UsageStore(local_path).record(record)
            UsageStore(other_path).record({**record,'model':'other-proxy-model','input_tokens':1000000})
            now=datetime(2026,10,10,10,tzinfo=timezone.utc)
            restarted=UsageStore(local_path)
            restarted.record({**record,'id':'request-2','timestamp':'2026-10-10T09:00:00.000Z'})
            report=restarted.report(now=now)
            self.assertEqual(report['source'],'local-proxy')
            self.assertEqual(report['totals']['requests'],2)
            self.assertEqual(report['totals']['total_tokens'],34)
            self.assertEqual(report['recording_since'],record['timestamp'])
            self.assertEqual(report['available_models'],['claude-opus-5'])
            self.assertEqual({row['model'] for row in report['models']+report['events']},{'claude-opus-5'})
            self.assertEqual(sum(row['total_tokens'] for row in report['trend']),34)
            self.assertEqual(UsageStore(other_path).report(now=now)['totals']['requests'],1)


if __name__=='__main__': unittest.main()
