# TODO

## Web interface to manage linked accounts

Accounts are managed only with the `contacts-mcp auth` CLI on the server (run as a one-shot
container against the data volume, then `SIGHUP` the server). A small web page served by the
same process would make this easier:

- List accounts with e-mail, status (ok, needs re-link, unreadable) and read-only flag.
- Link or re-link an account through Google consent in the browser.
- Remove an account (revoking its token at Google) and toggle read-only.
- Apply changes to the running server directly, without a SIGHUP or a restart.

Constraints:

- Behind the same OIDC sign-in and `CONTACTS_MCP_ALLOWED_USERS` allowlist as `/mcp`; only
  available in HTTP mode.
- Linking from the web needs a redirect the server handles itself
  (`<base_url>/accounts/callback` or similar), so likely a Google OAuth client of type
  *Web application* next to, or instead of, today's *Desktop app* client (whose loopback
  redirect is what the CLI uses). Decide whether to support both.
- Writes go through `AccountFileStore` (atomic, 0600) and then `AccountRegistry.reload()`.
- CSRF protection on the form posts; same Host/Origin protection as the rest of the server.
