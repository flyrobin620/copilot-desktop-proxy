"""Resolve stable, user-selectable aliases against the live desktop model catalog."""
import json
import re

ALIASES = {'copilot-auto': 'opus', 'copilot-opus': 'opus',
           'copilot-sonnet': 'sonnet', 'copilot-haiku': 'haiku'}

# Client configuration policy, not a claim about upstream capacity.
# All models default to 1M. Add exact model IDs here only to opt them out;
# dotted and hyphenated versions are treated as the same model.
CONTEXT_1M_DENYLIST = frozenset()


def use_1m_context(model):
    return client_model_id(model) not in {client_model_id(value) for value in CONTEXT_1M_DENYLIST}


def default_client_model_id(model):
    return client_model_id(model) + ('[1m]' if use_1m_context(model) else '')


def default_auto_model(catalog):
    try:
        latest = resolve_model('copilot-auto', catalog)
    except ValueError:
        return 'copilot-auto[1m]'
    return 'copilot-auto[1m]' if use_1m_context(latest) else 'copilot-auto'


def client_model_id(model):
    """Return the bare, hyphenated ID used for capability lookup and matching."""
    model = re.sub(r'\[1m\]$', '', model, flags=re.IGNORECASE)
    if re.fullmatch(r'claude-(?:opus|sonnet|haiku)-\d+(?:\.\d+)+', model):
        return model.replace('.', '-')
    return model


def resolve_model(model, catalog):
    # [1m] is a Claude Code context hint, never part of a provider's model ID.
    model = re.sub(r'\[1m\]$', '', model, flags=re.IGNORECASE)
    family = ALIASES.get(model)
    if not family:
        if any(entry['model_id'] == model for entry in catalog):
            return model
        for entry in catalog:
            if client_model_id(entry['model_id']) == model:
                return entry['model_id']
        return model
    candidates = []
    for entry in catalog:
        name = entry['model_id']
        match = re.fullmatch(r'claude-' + family + r'-(\d+(?:[.-]\d+)*)(?:-latest)?', name)
        if match:
            version = tuple(int(n) for n in re.split(r'[.-]', match[1]) if len(n) < 6)
            candidates.append((version, name))
    if not candidates:
        raise ValueError('The desktop catalog has no supported ' + family + ' model.')
    return max(candidates)[1]


def replace_model(raw, model):
    """Replace only a top-level model string; all other JSON bytes stay identical."""
    text = raw.decode('utf-8')
    decoder = json.JSONDecoder()
    index = text.index('{') + 1
    while True:
        while text[index].isspace() or text[index] == ',': index += 1
        if text[index] == '}': break
        key, end = decoder.raw_decode(text, index)
        index = end
        while text[index].isspace(): index += 1
        if text[index] != ':': raise ValueError('Invalid JSON field.')
        index += 1
        while text[index].isspace(): index += 1
        _, end = decoder.raw_decode(text, index)
        if key == 'model':
            return (text[:index] + json.dumps(model) + text[end:]).encode('utf-8')
        index = end
    raise ValueError('A model field is required.')
