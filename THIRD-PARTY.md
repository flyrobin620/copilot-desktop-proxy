# Third-party software

MIT covers this repository's proxy source and documentation. It does not relicense Microsoft Copilot,
Claude Code, Claude Desktop, Python or their components.

- **Python**: downloaded from [python.org](https://www.python.org/downloads/windows/).
  Its license is installed as `runtime/python/LICENSE.txt`. Only `python*._pth` is adjusted to import proxy modules.
- **Claude Code**: downloaded from Anthropic's official `downloads.claude.ai` distribution; it remains
  Anthropic software under its applicable terms. See [setup documentation](https://code.claude.com/docs/en/setup).
- **Claude Desktop** and **Microsoft Copilot**: installed separately by the user; neither is included.

`package-lock.json` pins versions and official URLs. The installer downloads components on the user's
computer. Git and the source ZIP contain no vendor executables, accounts, API keys, sign-in data or usage history.
