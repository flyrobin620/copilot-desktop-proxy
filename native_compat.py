"""Narrow compatibility adjustments for the Copilot Loki Messages gateway."""
import json
from model_selection import client_model_id


KEEP_ALL_THINKING = {'edits': [{'type': 'clear_thinking_20251015', 'keep': 'all'}]}

# Temporary workaround, reproduced on 2026-10-16 only for DF Opus 5.5.
# Keys are the actual upstream hostname and canonical, resolved model ID.
# Do not extend this to PROD, whole model families or future Auto targets without
# evidence. See README.md's context_management section for the reproduction and
# retirement procedure: disable loki_context_management_compat, verify that the
# affected server accepts the original directive, then remove this entry.
KEEP_ALL_WORKAROUNDS = frozenset({('df.loki.delve.office.com', 'claude-opus-5-5')})

# Temporary, independently switchable workaround for the 2026-10-17 paired
# controls (including the affected machine): streaming adaptive thinking plus
# display=updates AND this exact beta produced 400; omitting both produced 200.
# Neither component was tested alone. Do not broaden to other models, backends,
# non-streaming/enabled thinking, future beta versions or unknown control fields.
# See README for the evidence and server-fix retirement procedure.
DISPLAY_UPDATES_BETA = 'thinking-display-updates-2026-08-18'
UPDATES_THINKING = {'type': 'adaptive', 'display': 'updates'}
THINKING_DISPLAY_WORKAROUNDS = frozenset({('df.loki.delve.office.com', 'claude-opus-5-5')})

# Paired live control on 2026-11-04: PROD Opus 5.5 rejected ttl=1h with
# "Long-lived prompt caching ... is not supported"; identical ttl=5m returned 200.
CACHE_TTL_WORKAROUNDS = frozenset({('loki.delve.office.com', 'claude-opus-5-5')})


def adapt_cache_ttl(raw, payload, *, upstream_host, resolved_model, enabled=True):
    """Shorten known unsupported 1h protocol cache markers, preserving other bytes.

    Visit only Messages cache locations, never user text, tool input/schema or
    document sources. No retry, mutation of the caller's object or cache removal.
    Ambiguous JSON and unknown TTLs pass through for upstream validation.
    """
    unchanged = (raw, payload, False)
    if (not enabled or not isinstance(resolved_model, str) or
            (upstream_host, client_model_id(resolved_model)) not in CACHE_TTL_WORKAROUNDS):
        return unchanged
    try:
        text = raw.decode('utf-8')
        decoder = json.JSONDecoder()
        replacements = []

        def array(index, kind):
            if text[index] != '[':
                return
            index += 1
            while True:
                while text[index].isspace() or text[index] == ',':
                    index += 1
                if text[index] == ']':
                    return
                visit(index, kind)
                _, index = decoder.raw_decode(text, index)

        def visit(index, kind):
            if text[index] != '{':
                return
            members = _object_members(text, index)
            fields = {key: (start, end) for key, _, start, end in members}
            if len(fields) != len(members):
                raise ValueError('Ambiguous protocol object.')
            def value(key):
                start, end = fields[key]
                return json.loads(text[start:end])
            if kind == 'cache':
                if ('type' in fields and value('type') == 'ephemeral' and
                        'ttl' in fields and value('ttl') == '1h'):
                    replacements.append(fields['ttl'])
                return
            if kind != 'message' and 'cache_control' in fields:
                visit(fields['cache_control'][0], 'cache')
            if kind == 'root':
                for key, child in (('system', 'block'), ('tools', 'tool'), ('messages', 'message')):
                    if key in fields:
                        array(fields[key][0], child)
            elif 'content' in fields and (kind == 'message' or
                    (kind == 'block' and 'type' in fields and value('type') == 'tool_result')):
                array(fields['content'][0], 'block')

        visit(len(text) - len(text.lstrip()), 'root')
        if not replacements:
            return unchanged
        for start, end in sorted(replacements, reverse=True):
            text = text[:start] + '"5m"' + text[end:]
        updated = text.encode('utf-8')
        return updated, json.loads(updated), True
    except (ValueError, IndexError, RecursionError):
        return unchanged


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Ambiguous JSON object.')
        result[key] = value
    return result


def _remove_keep_all_field(raw):
    """Remove one exact top-level directive, preserving every other value's bytes.

    Refuse duplicate top-level fields and duplicate keys within the directive.
    The input has already passed JSON validation; failures leave it untouched.
    """
    text = raw.decode('utf-8')
    decoder = json.JSONDecoder()
    index = text.index('{') + 1
    members = []
    while True:
        while text[index].isspace() or text[index] == ',':
            index += 1
        if text[index] == '}':
            break
        start = index
        key, index = decoder.raw_decode(text, index)
        while text[index].isspace():
            index += 1
        if text[index] != ':':
            raise ValueError('Invalid JSON field.')
        index += 1
        while text[index].isspace():
            index += 1
        value_start = index
        _, index = decoder.raw_decode(text, index)
        members.append((key, start, value_start, index))
    keys = [member[0] for member in members]
    if len(set(keys)) != len(keys) or 'context_management' not in keys:
        return None
    position = keys.index('context_management')
    _, start, value_start, end = members[position]
    value = json.loads(text[value_start:end], object_pairs_hook=_unique_object)
    if value != KEEP_ALL_THINKING:
        return None
    if position < len(members) - 1:
        end = members[position + 1][1]
    elif position:
        start = members[position - 1][3]
    return (text[:start] + text[end:]).encode('utf-8')


def adapt_context_management(raw, payload, *, upstream_host, resolved_model, enabled=True):
    """Omit the exact keep-all directive only for a known affected backend/model.

    The caller supplies the actual upstream host and model after alias resolution.
    New models and other environments pass through unless explicitly listed.

    Messages, thinking blocks/signatures, tools, cache markers and beta headers
    are not changed. Omission delegates thinking retention to the upstream's
    default; it does not emulate support for the server-side keep-all feature.
    """
    if not enabled or not isinstance(resolved_model, str):
        return raw, payload, False
    if ((upstream_host, client_model_id(resolved_model)) not in KEEP_ALL_WORKAROUNDS
            or payload.get('context_management') != KEEP_ALL_THINKING):
        return raw, payload, False
    try:
        updated = _remove_keep_all_field(raw)
    except (ValueError, IndexError, RecursionError):
        return raw, payload, False
    if updated is None:
        return raw, payload, False
    forwarded = dict(payload)
    del forwarded['context_management']
    return updated, forwarded, True


def _object_members(text, index):
    """Locate original character spans; never serialize unrelated JSON values."""
    if text[index] != '{':
        raise ValueError('Expected object.')
    decoder, members = json.JSONDecoder(), []
    index += 1
    while True:
        while text[index].isspace() or text[index] == ',':
            index += 1
        if text[index] == '}':
            return members
        start = index
        key, index = decoder.raw_decode(text, index)
        while text[index].isspace():
            index += 1
        if text[index] != ':':
            raise ValueError('Invalid JSON field.')
        index += 1
        while text[index].isspace():
            index += 1
        value_start = index
        _, index = decoder.raw_decode(text, index)
        members.append((key, start, value_start, index))


def _remove_updates_display(raw):
    text = raw.decode('utf-8')
    members = _object_members(text, text.index('{'))
    keys = [member[0] for member in members]
    if len(set(keys)) != len(keys) or 'thinking' not in keys:
        return None
    _, _, value_start, end = members[keys.index('thinking')]
    if json.loads(text[value_start:end], object_pairs_hook=_unique_object) != UPDATES_THINKING:
        return None
    thinking_members = _object_members(text, value_start)
    position = next(i for i, member in enumerate(thinking_members) if member[0] == 'display')
    _, start, _, end = thinking_members[position]
    if position < len(thinking_members) - 1:
        end = thinking_members[position + 1][1]
    else:
        start = thinking_members[position - 1][3]
    return (text[:start] + text[end:]).encode('utf-8')


def adapt_thinking_display(raw, payload, beta_header, *, upstream_host, resolved_model, enabled=True):
    """Omit the reproduced display/beta pair; preserve adaptive thinking itself.

    The caller limits this to native Messages after concrete model resolution.
    This is a pre-send transformation, not a retry or a generic 400 handler.
    No input object is mutated; failures/ambiguous shapes leave both bytes and
    header unchanged so the two halves cannot be modified independently.
    """
    unchanged = (raw, payload, beta_header, False)
    if not enabled or not isinstance(resolved_model, str) or not isinstance(beta_header, str):
        return unchanged
    if ((upstream_host, client_model_id(resolved_model)) not in THINKING_DISPLAY_WORKAROUNDS
            or payload.get('stream') is not True or payload.get('thinking') != UPDATES_THINKING):
        return unchanged
    tokens = beta_header.split(',')
    if not any(token.strip() == DISPLAY_UPDATES_BETA for token in tokens):
        return unchanged
    # A future version may attach different semantics to the same display value.
    if any(token.strip().startswith('thinking-display-updates-') and
           token.strip() != DISPLAY_UPDATES_BETA for token in tokens):
        return unchanged
    try:
        updated = _remove_updates_display(raw)
    except (ValueError, IndexError, RecursionError):
        return unchanged
    if updated is None:
        return unchanged
    forwarded = dict(payload)
    forwarded['thinking'] = {'type': 'adaptive'}
    beta = ','.join(token for token in tokens if token.strip() != DISPLAY_UPDATES_BETA)
    return updated, forwarded, beta, True
