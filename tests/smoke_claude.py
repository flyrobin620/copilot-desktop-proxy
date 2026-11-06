"""Live test using installed Claude Code, with only Read allowed on a local test fixture."""
import json
from pathlib import Path
import subprocess
import os
import shutil

root = Path(__file__).resolve().parent
executable = os.environ.get('COPILOT_PROXY_CLAUDE')
if not executable:
    bundled = root / 'runtime/claude/claude.exe'
    executable = str(bundled) if bundled.is_file() else shutil.which('claude')
if not executable:
    raise SystemExit('Claude Code is missing. Use the full portable package or install Claude Code.')
result = subprocess.run([str(executable), '--settings', str(root / 'claude-settings.json'),
    '--model', 'claude-opus-5', '--effort', 'xhigh', '-p',
    'Read claude-smoke-fixture.txt using the Read tool, then respond with exactly its content.',
    '--no-session-persistence', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
    '--disable-slash-commands', '--tools', 'Read', '--allowedTools', 'Read',
    '--permission-mode', 'dontAsk', '--setting-sources', '', '--output-format', 'stream-json',
    '--verbose', '--include-partial-messages'], cwd=root, text=True, encoding='utf-8',
    capture_output=True, timeout=180)
events = []
for line in result.stdout.splitlines():
    try:
        events.append(json.loads(line))
    except ValueError:
        continue
tools = [b.get('name') for e in events if e.get('type') == 'assistant'
         for b in e.get('message', {}).get('content', []) if b.get('type') == 'tool_use']
final = next((e for e in reversed(events) if e.get('type') == 'result'), {})
report = {'exit_code': result.returncode, 'stream_events': sum(e.get('type') == 'stream_event' for e in events),
          'read_tool_called': 'Read' in tools, 'answer_matches_fixture': 'CLAUDE_CODE_TOOL_OK_7F3A' in final.get('result', ''),
          'is_error': final.get('is_error', True)}
print(json.dumps(report))
if result.returncode or report['is_error'] or not report['read_tool_called'] or not report['answer_matches_fixture']:
    if isinstance(final.get('result'), str):
        print(final['result'][:1000])
    raise SystemExit(1)
