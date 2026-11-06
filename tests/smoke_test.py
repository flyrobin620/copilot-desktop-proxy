"""Explicit live validation; sends small test prompts using the Copilot account."""
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = 'http://127.0.0.1:8787'


def request(path, payload=None, extra_headers=None):
    headers = {'Content-Type': 'application/json'}
    headers.update(extra_headers or {})
    data = json.dumps(payload).encode() if payload is not None else None
    return urlopen(Request(BASE + path, data=data, headers=headers), timeout=180)


def chat(payload):
    with request('/v1/chat/completions', payload) as response:
        return json.load(response)


def stream(payload):
    chunks, done = [], False
    with request('/v1/chat/completions', {**payload, 'stream': True, 'stream_options': {'include_usage': True}}) as response:
        assert response.headers['Content-Type'].startswith('text/event-stream')
        for line in response:
            if not line.startswith(b'data: '):
                continue
            text = line[6:].strip()
            if text == b'[DONE]':
                done = True
            else:
                chunk = json.loads(text)
                assert 'error' not in chunk, chunk.get('error', {}).get('code')
                chunks.append(chunk)
    assert done, 'No stream completion marker'
    assert chunks[-1]['usage']['total_tokens'] > 0
    return chunks


with request('/health') as response:
    assert json.load(response)['status'] == 'ready'
with request('/v1/models') as response:
    assert 'claude-opus-5' in [m['id'] for m in json.load(response)['data']]
print('PASS health and model catalog', flush=True)

for payload, headers, status in [({'messages': []}, {}, 400),
                                 ({'messages': []}, {'Origin': 'https://example.com'}, 403)]:
    try:
        request('/v1/chat/completions', payload, headers)
        raise AssertionError('Invalid request was accepted')
    except HTTPError as error:
        assert error.code == status
        error.close()
print('PASS invalid requests and browser-origin protection', flush=True)

basic = {'model': 'claude-opus-5', 'max_tokens': 1024,
         'messages': [{'role': 'user', 'content': 'Reply with exactly PROXY_OK.'}]}
result = chat(basic)
assert result['model'] == 'claude-opus-5'
assert 'PROXY_OK' in result['choices'][0]['message']['content']
print('PASS real Opus 5, default xhigh reasoning, non-stream completion', flush=True)

chunks = stream({**basic, 'reasoning_effort': 'none'})
content = ''.join(c['choices'][0]['delta'].get('content', '') for c in chunks if c['choices'])
assert 'PROXY_OK' in content
print('PASS real SSE text and usage', flush=True)

tool_request = {'model': 'claude-opus-5', 'max_tokens': 256,
    'messages': [{'role': 'user', 'content': 'Use add to compute 2 + 3.'}],
    'tools': [{'type': 'function', 'function': {'name': 'add', 'description': 'Add two integers.',
               'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}},
                              'required': ['a', 'b'], 'additionalProperties': False}}}],
    'tool_choice': {'type': 'function', 'function': {'name': 'add'}}}
result = chat(tool_request)
message = result['choices'][0]['message']
assert result['choices'][0]['finish_reason'] == 'tool_calls'
call = message['tool_calls'][0]
args = json.loads(call['function']['arguments'])
assert args == {'a': 2, 'b': 3}
print('PASS real function tool call', flush=True)

follow_up = {**tool_request, 'reasoning_effort': 'none', 'tool_choice': 'auto',
             'messages': tool_request['messages'] + [message,
                 {'role': 'tool', 'tool_call_id': call['id'], 'content': str(args['a'] + args['b'])}]}
result = chat(follow_up)
assert '5' in result['choices'][0]['message']['content']
print('PASS tool result and multi-turn conversation', flush=True)

chunks = stream(tool_request)
tool_chunks = [t for c in chunks if c['choices'] for t in c['choices'][0]['delta'].get('tool_calls', [])]
assert tool_chunks[0]['function']['name'] == 'add'
assert json.loads(''.join(t['function'].get('arguments', '') for t in tool_chunks)) == {'a': 2, 'b': 3}
print('PASS streamed function arguments', flush=True)
