"""Claude Desktop's gateway catalog (separate from Claude Code CLI hints)."""
import re
from urllib.parse import urlsplit, urlunsplit

from model_selection import client_model_id, resolve_model, use_1m_context


DESKTOP_BASE_PATH = '/desktop'
MESSAGE_PATHS = frozenset(('/v1/messages', '/v1/messages/count_tokens'))


def desktop_message_path(target):
    """Map only supported Desktop message paths; preserve the query verbatim."""
    route = urlsplit(target)
    path = route.path.removeprefix(DESKTOP_BASE_PATH)
    if route.path.startswith(DESKTOP_BASE_PATH + '/') and path in MESSAGE_PATHS:
        return urlunsplit(('', '', path, route.query, ''))
    return target


def desktop_catalog(catalog):
    """Bare, concrete IDs avoid duplicate [1m] suffixes and stale tier defaults.

    supports_1m follows the user's existing client-window policy, not a claim
    that every upstream model has passed a million-token inference test.
    """
    defaults = {}
    for family in ('opus', 'sonnet', 'haiku'):
        try:
            defaults[family] = resolve_model('copilot-' + family, catalog)
        except ValueError:
            pass
    rows = []
    seen = set()
    for model in catalog:
        name = model['model_id']
        client_id = client_model_id(name)
        if client_id in seen:
            continue
        seen.add(client_id)
        family = re.match(r'^claude-(opus|sonnet|haiku)-', client_id)
        row = {'id': client_id, 'object': 'model', 'type': 'model',
               'display_name': name.replace('claude-', 'Claude ').replace('-', ' ').title(),
               'description': 'Copilot desktop provider; fixed model version',
               'supports_1m': use_1m_context(name)}
        if family:
            tier = family[1]
            row.update(anthropic_family_tier=tier,
                       is_family_default=client_id == client_model_id(defaults.get(tier, '')))
        rows.append(row)
    # The first discovered model is the initial default. Keep actual model IDs
    # so Desktop can derive capabilities; do not advertise CLI-only aliases.
    rows.sort(key=lambda row: (
        0 if row['id'] == client_model_id(defaults.get('opus', '')) else 1,
        ('opus', 'sonnet', 'haiku', '').index(row.get('anthropic_family_tier', '')),
        not row.get('is_family_default', False), row['id']))
    return {'object': 'list', 'has_more': False,
            'first_id': rows[0]['id'] if rows else None,
            'last_id': rows[-1]['id'] if rows else None, 'data': rows}
