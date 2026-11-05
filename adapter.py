"""OpenAI Chat Completions <-> Anthropic Messages, including SSE tool calls."""
import json
import time
import uuid


class InvalidRequest(ValueError):
    pass


def content_blocks(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    if not isinstance(content, list):
        raise InvalidRequest('Message content must be a string or an array.')
    blocks = []
    for part in content:
        if not isinstance(part, dict):
            raise InvalidRequest('Content parts must be objects.')
        if part.get('type') == 'text' and isinstance(part.get('text'), str):
            blocks.append({'type': 'text', 'text': part['text']})
        elif part.get('type') == 'image_url':
            image = part.get('image_url')
            if not isinstance(image, dict) or not isinstance(image.get('url'), str):
                raise InvalidRequest('image_url requires an object containing a URL string.')
            url = image['url']
            if url.startswith('data:image/') and ';base64,' in url:
                meta, data = url.split(';base64,', 1)
                source = {'type': 'base64', 'media_type': meta[5:], 'data': data}
            elif url.startswith('https://'):
                source = {'type': 'url', 'url': url}
            else:
                raise InvalidRequest('Images require an HTTPS URL or a base64 data URL.')
            blocks.append({'type': 'image', 'source': source})
        else:
            raise InvalidRequest('Unsupported message content type.')
    return blocks


def to_anthropic(request, catalog, default_effort='xhigh'):
    if not isinstance(request, dict):
        raise InvalidRequest('The JSON body must be an object.')
    model = request.get('model') or 'claude-opus-5'
    models = {m['model_id']: m for m in catalog}
    if not isinstance(model, str) or model not in models:
        raise InvalidRequest('Unknown model. Use GET /v1/models for the available models.')
    if request.get('n', 1) != 1:
        raise InvalidRequest('Only n=1 is supported.')
    for key in ('logprobs', 'top_logprobs', 'logit_bias', 'seed', 'audio', 'modalities',
                'prediction', 'functions', 'function_call', 'frequency_penalty', 'presence_penalty'):
        if request.get(key) is not None:
            raise InvalidRequest(f'{key} is not supported by this gateway.')
    if not isinstance(request.get('stream', False), bool):
        raise InvalidRequest('stream must be a boolean.')
    source_messages = request.get('messages')
    if not isinstance(source_messages, list) or not source_messages:
        raise InvalidRequest('messages must be a nonempty array.')
    messages, system = [], []
    for message in source_messages:
        if not isinstance(message, dict):
            raise InvalidRequest('Each message must be an object.')
        role = message.get('role')
        if role in ('system', 'developer'):
            blocks = content_blocks(message.get('content'))
            if any(b['type'] != 'text' for b in blocks):
                raise InvalidRequest('System and developer messages must contain text only.')
            system.extend(blocks)
            continue
        if role == 'tool':
            if not isinstance(message.get('tool_call_id'), str) or not message['tool_call_id']:
                raise InvalidRequest('Tool results require tool_call_id.')
            blocks = [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'],
                       'content': content_blocks(message.get('content'))}]
            role = 'user'
        elif role in ('user', 'assistant'):
            blocks = content_blocks(message.get('content'))
            calls = message.get('tool_calls') or []
            if not isinstance(calls, list) or (calls and role != 'assistant'):
                raise InvalidRequest('tool_calls are only valid in assistant messages.')
            for call in calls:
                try:
                    function = call['function']
                    arguments = json.loads(function['arguments'])
                    if call.get('type') != 'function' or not isinstance(arguments, dict):
                        raise ValueError()
                    blocks.append({'type': 'tool_use', 'id': call['id'],
                                   'name': function['name'], 'input': arguments})
                except (KeyError, TypeError, ValueError):
                    raise InvalidRequest('Tool calls require an id, function name, and JSON object arguments.') from None
        else:
            raise InvalidRequest('Unsupported message role.')
        if not blocks:
            raise InvalidRequest('Messages must contain content or tool calls.')
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'].extend(blocks)
        else:
            messages.append({'role': role, 'content': blocks})
    if not messages or messages[0]['role'] != 'user':
        raise InvalidRequest('The first non-system message must have role user.')
    max_tokens = request.get('max_completion_tokens', request.get('max_tokens', 8192))
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise InvalidRequest('max_tokens must be a positive integer.')
    result = {'model': model, 'max_tokens': max_tokens, 'messages': messages,
              'stream': request.get('stream', False)}
    if system:
        result['system'] = system
    if request.get('tools') is not None:
        if not isinstance(request['tools'], list):
            raise InvalidRequest('tools must be an array.')
        tools = []
        for tool in request['tools']:
            try:
                function = tool['function']
                if tool['type'] != 'function' or not isinstance(function['name'], str):
                    raise ValueError()
                tools.append({'name': function['name'], 'description': function.get('description', ''),
                              'input_schema': function.get('parameters', {'type': 'object', 'properties': {}})})
            except (KeyError, TypeError, ValueError):
                raise InvalidRequest('Only function tools are supported.') from None
        if tools:
            result['tools'] = tools
    choice = request.get('tool_choice')
    if choice is not None:
        if isinstance(choice, str) and choice in ('auto', 'none', 'required'):
            result['tool_choice'] = {'type': {'required': 'any'}.get(choice, choice)}
        elif isinstance(choice, dict) and choice.get('type') == 'function':
            function = choice.get('function')
            name = function.get('name') if isinstance(function, dict) else None
            if not isinstance(name, str) or not name:
                raise InvalidRequest('A forced tool choice requires a function name.')
            result['tool_choice'] = {'type': 'tool', 'name': name}
        else:
            raise InvalidRequest('Unsupported tool_choice.')
    if request.get('parallel_tool_calls') is not None:
        if not isinstance(request['parallel_tool_calls'], bool):
            raise InvalidRequest('parallel_tool_calls must be boolean.')
        result.setdefault('tool_choice', {'type': 'auto'})['disable_parallel_tool_use'] = not request['parallel_tool_calls']
    if 'tool_choice' in result and not result.get('tools'):
        if result['tool_choice']['type'] not in ('none', 'auto'):
            raise InvalidRequest('tool_choice requires tools.')
        result.pop('tool_choice')
    supported = json.loads(models[model].get('supported_reasoning_efforts') or '[]')
    default = default_effort if default_effort in supported else ('high' if 'high' in supported else 'none')
    forced_tool = result.get('tool_choice', {}).get('type') in ('any', 'tool')
    if forced_tool or 'temperature' in request or 'top_p' in request:
        default = 'none'
    effort = request.get('reasoning_effort', default)
    if effort is None:
        effort = default
    if effort != 'none':
        if not isinstance(effort, str) or effort not in supported:
            raise InvalidRequest('Unsupported reasoning_effort for this model.')
        if forced_tool or 'temperature' in request or 'top_p' in request:
            raise InvalidRequest('Explicit reasoning cannot be combined with forced tools or sampling controls.')
        result['thinking'] = {'type': 'adaptive'}
        result['output_config'] = {'effort': effort}
    for key in ('temperature', 'top_p'):
        if key in request:
            result[key] = request[key]
    if request.get('stop') is not None:
        stops = request['stop']
        if isinstance(stops, str):
            stops = [stops]
        if not isinstance(stops, list) or any(not isinstance(s, str) for s in stops):
            raise InvalidRequest('stop must be a string or array of strings.')
        result['stop_sequences'] = stops
    response_format = request.get('response_format')
    if response_format and response_format != {'type': 'text'}:
        if not isinstance(response_format, dict) or response_format.get('type') != 'json_schema':
            raise InvalidRequest('Structured output requires response_format.type=json_schema.')
        definition = response_format.get('json_schema')
        schema = definition.get('schema') if isinstance(definition, dict) else None
        if not isinstance(schema, dict):
            raise InvalidRequest('A JSON schema is required.')
        result.setdefault('output_config', {})['format'] = {'type': 'json_schema', 'schema': schema}
    options = request.get('stream_options') or {}
    if not isinstance(options, dict):
        raise InvalidRequest('stream_options must be an object.')
    if 'include_usage' in options and not isinstance(options['include_usage'], bool):
        raise InvalidRequest('stream_options.include_usage must be boolean.')
    return result


def finish_reason(reason):
    return {'tool_use': 'tool_calls', 'max_tokens': 'length', 'refusal': 'content_filter'}.get(reason, 'stop')


def usage(data):
    prompt = sum(data.get(k, 0) or 0 for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
    completion = data.get('output_tokens', 0) or 0
    return {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': prompt + completion,
            'prompt_tokens_details': {'cached_tokens': data.get('cache_read_input_tokens', 0) or 0}}


def completion(response, model):
    text, reasoning, calls = [], [], []
    for block in response.get('content', []):
        if block['type'] == 'text':
            text.append(block.get('text', ''))
        elif block['type'] == 'thinking':
            reasoning.append(block.get('thinking', ''))
        elif block['type'] == 'tool_use':
            calls.append({'id': block['id'], 'type': 'function', 'function': {
                'name': block['name'], 'arguments': json.dumps(block.get('input', {}), ensure_ascii=False)}})
    message = {'role': 'assistant', 'content': ''.join(text) or (None if calls else '')}
    if reasoning:
        message['reasoning_content'] = ''.join(reasoning)
    if calls:
        message['tool_calls'] = calls
    return {'id': 'chatcmpl-' + response.get('id', uuid.uuid4().hex), 'object': 'chat.completion',
            'created': int(time.time()), 'model': response.get('model', model),
            'choices': [{'index': 0, 'message': message, 'finish_reason': finish_reason(response.get('stop_reason'))}],
            'usage': usage(response.get('usage', {}))}


def iter_sse(response):
    data = []
    event_bytes = 0
    while True:
        line = response.readline(1024 * 1024 + 1)
        if len(line) > 1024 * 1024:
            raise RuntimeError('Upstream SSE event exceeded the size limit.')
        if not line:
            if data:
                yield json.loads('\n'.join(data))
            return
        line_bytes = len(line)
        line = line.decode('utf-8').rstrip('\r\n')
        if not line:
            if data:
                yield json.loads('\n'.join(data))
                data = []
                event_bytes = 0
        elif line.startswith('data:'):
            event_bytes += line_bytes
            if event_bytes > 1024 * 1024:
                raise RuntimeError('Upstream SSE event exceeded the size limit.')
            data.append(line[5:].lstrip(' '))


class StreamAdapter:
    def __init__(self, model, include_usage=False):
        self.model, self.include_usage = model, include_usage
        self.id, self.created = 'chatcmpl-' + uuid.uuid4().hex, int(time.time())
        self.tools, self.token_usage, self.finished = {}, {}, False

    def chunk(self, delta, reason=None, choices=True):
        return {'id': self.id, 'object': 'chat.completion.chunk', 'created': self.created,
                'model': self.model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}] if choices else []}

    def feed(self, event):
        kind = event.get('type')
        if kind == 'message_start':
            message = event['message']
            self.id = 'chatcmpl-' + message['id']
            self.model = message.get('model', self.model)
            self.token_usage.update(message.get('usage', {}))
            return [self.chunk({'role': 'assistant', 'content': ''})]
        if kind == 'content_block_start':
            block = event['content_block']
            if block['type'] == 'tool_use':
                index = len(self.tools)
                self.tools[event['index']] = index
                return [self.chunk({'tool_calls': [{'index': index, 'id': block['id'], 'type': 'function',
                    'function': {'name': block['name'], 'arguments': ''}}]})]
            if block['type'] == 'text' and block.get('text'):
                return [self.chunk({'content': block['text']})]
        if kind == 'content_block_delta':
            delta = event['delta']
            if delta['type'] == 'text_delta':
                return [self.chunk({'content': delta['text']})]
            if delta['type'] == 'thinking_delta':
                return [self.chunk({'reasoning_content': delta['thinking']})]
            if delta['type'] == 'input_json_delta':
                return [self.chunk({'tool_calls': [{'index': self.tools[event['index']],
                    'function': {'arguments': delta['partial_json']}}]})]
        if kind == 'message_delta':
            self.token_usage.update(event.get('usage', {}))
            reason = event.get('delta', {}).get('stop_reason')
            if reason:
                return [self.chunk({}, finish_reason(reason))]
        if kind == 'message_stop':
            self.finished = True
            if self.include_usage:
                chunk = self.chunk({}, choices=False)
                chunk['usage'] = usage(self.token_usage)
                return [chunk]
        return []
