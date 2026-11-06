import io
import json
import unittest
from adapter import InvalidRequest, StreamAdapter, completion, iter_sse, to_anthropic

CATALOG = [{'model_id': 'claude-opus-5', 'supported_reasoning_efforts': '["low","high","xhigh"]'}]


class AdapterTests(unittest.TestCase):
    def test_round_trip_tool_conversation(self):
        request = {'model': 'claude-opus-5', 'reasoning_effort': 'none', 'messages': [
            {'role': 'developer', 'content': 'Use the calculator.'},
            {'role': 'user', 'content': 'Add 2 and 3.'},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_1', 'type': 'function', 'function': {'name': 'add', 'arguments': '{"a":2,"b":3}'}}]},
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': '5'}]}
        upstream = to_anthropic(request, CATALOG)
        self.assertEqual(upstream['messages'][1]['content'][0]['input'], {'a': 2, 'b': 3})
        self.assertEqual(upstream['messages'][2]['content'][0]['tool_use_id'], 'call_1')
        self.assertEqual(upstream['system'][0]['text'], 'Use the calculator.')
        response = completion({'id': 'test_message', 'model': 'claude-opus-5', 'content': [
            {'type': 'text', 'text': '5'}], 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 3, 'cache_read_input_tokens': 4, 'output_tokens': 2}}, 'claude-opus-5')
        self.assertEqual(response['choices'][0]['message']['content'], '5')
        self.assertEqual(response['usage']['total_tokens'], 9)

    def test_stream_tool_indexes_skip_thinking_blocks(self):
        adapter = StreamAdapter('claude-opus-5', include_usage=True)
        events = [
            {'type': 'message_start', 'message': {'id': 'test', 'usage': {'input_tokens': 5}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'call_1', 'name': 'add'}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"a":'}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '2}'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 6}},
            {'type': 'message_stop'}]
        chunks = [chunk for event in events for chunk in adapter.feed(event)]
        calls = [c['choices'][0]['delta']['tool_calls'][0] for c in chunks if c['choices'] and 'tool_calls' in c['choices'][0]['delta']]
        self.assertTrue(all(call['index'] == 0 for call in calls))
        self.assertEqual(json.loads(''.join(c['function'].get('arguments', '') for c in calls)), {'a': 2})
        self.assertEqual(chunks[-2]['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(chunks[-1]['usage']['total_tokens'], 11)
        self.assertTrue(adapter.finished)

    def test_unicode_sse_and_incomplete_stream(self):
        wire = ': heartbeat\r\nevent: content_block_delta\r\ndata: {"type":"content_block_delta","index":0,\r\ndata: "delta":{"type":"text_delta","text":"你好"}}\r\n\r\n'
        events = list(iter_sse(io.BytesIO(wire.encode())))
        adapter = StreamAdapter('claude-opus-5')
        chunk = adapter.feed(events[0])[0]
        self.assertEqual(chunk['choices'][0]['delta']['content'], '你好')
        self.assertFalse(adapter.finished)

    def test_required_tool_does_not_enable_incompatible_default_thinking(self):
        result = to_anthropic({'messages': [{'role': 'user', 'content': 'Compute.'}],
            'tools': [{'type': 'function', 'function': {'name': 'add'}}], 'tool_choice': 'required'}, CATALOG)
        self.assertNotIn('thinking', result)
        self.assertEqual(result['tool_choice']['type'], 'any')

    def test_invalid_requests(self):
        for request in [[], {'messages': []}, {'messages': [{'role': 'tool', 'content': 'value'}]},
                        {'messages': [{'role': 'user', 'content': 'Hi'}], 'model': 'unknown'},
                        {'messages': [{'role': 'user', 'content': 'Hi'}], 'n': 2},
                        {'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': 'invalid'}]}]},
                        {'messages': [{'role': 'user', 'content': 'Hi'}], 'response_format': {'type': 'json_schema', 'json_schema': 'invalid'}},
                        {'messages': [{'role': 'user', 'content': 'Hi'}], 'max_tokens': -1}]:
            with self.subTest(request=request), self.assertRaises(InvalidRequest):
                to_anthropic(request, CATALOG)


if __name__ == '__main__':
    unittest.main()
