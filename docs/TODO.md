# TODO

## Link accounts from the settings page

The settings page (`CONTACTS_MCP_SETTINGS_PAGE`) lists accounts, toggles read-only and removes
them, but linking or re-linking still needs `contacts-mcp auth add` on the server and a
`SIGHUP`. Linking from the browser needs a redirect the server handles itself, so a Google OAuth
client of type *Web application* next to today's *Desktop app* client (whose loopback redirect
is what the CLI uses), and each account file has to record which client linked it.
