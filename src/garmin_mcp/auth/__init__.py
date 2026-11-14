"""Built-in OAuth 2.1 authorization server for a single user.

Follows the MCP authorization specification so any standard MCP client can
connect (not only Claude):

- `provider.py`  the OAuth logic the MCP SDK calls into (codes, tokens, clients)
- `clients.py`   how clients identify themselves (registration and metadata documents)
- `tokens.py`    signed, self-contained tokens (nothing is stored)
- `github.py`    the pages the browser visits: GitHub login and the consent screen

Design constraint: the container scales to zero and has no database, so nothing
may live in memory or on disk between requests. Every piece of state is a
signed token carried by the client or the browser.
"""
