# ContactsMCP

A self-hostable [MCP](https://modelcontextprotocol.io/) server for Google Contacts (the People
API), with several Google accounts side by side. Connect it to claude.ai (custom connector),
Claude Code or any other MCP client, and let the assistant search, read, create and edit your
contacts.

Contents: [1. What it does](#1-what-it-does) · [2. Google Cloud setup](#2-google-cloud-setup) ·
[3. Linking accounts](#3-linking-accounts) · [Configuration](#configuration) ·
[Development](#development) · [License](#license)

## 1. What it does

Every tool except `accounts` takes an `account` (the alias you chose when linking). It may be omitted only when
exactly one account is linked. `search_contacts` also accepts `"*"` to search every account at
once; results are tagged with their alias.

| Tool | Kind | What it does |
| --- | --- | --- |
| `accounts` | read | Linked accounts: alias, e-mail, read-only flag, status (ok, needs re-link, unreadable). Call it first. |
| `search_contacts` | read | Search contacts by name, e-mail, phone or organization (`people:searchContacts`). Compact results. |
| `list_contacts` | read | Page through all contacts, with sorting. |
| `get_contact` | read | One contact in full, with its `etag`. |
| `list_other_contacts` | read | The "Other contacts" Google collects automatically (people you e-mailed). |
| `search_other_contacts` | read | Search "Other contacts". |

Every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint`) and refuses arguments it does not know, naming them.

Things worth knowing: Google's search index lags behind writes (a contact you just created may
not be found for a while; `get_contact` is immediate), and contact text, "Other contacts" above
all, is third-party data: the server tells the model to treat it as data, never as instructions.

## 2. Google Cloud setup

One Google Cloud OAuth client is shared by all the accounts you link.

1. In the [Google Cloud console](https://console.cloud.google.com/) create a project (or pick
   one).
2. **Enable the People API**: *APIs & Services → Library → Google People API → Enable*.
3. **Configure the OAuth consent screen** (*Google Auth Platform* or *APIs & Services → OAuth
   consent screen*): app name, support e-mail, and for audience *External* (see below for
   Workspace). On the data access / scopes page add these scopes:
   - `https://www.googleapis.com/auth/contacts`
   - `https://www.googleapis.com/auth/contacts.other.readonly`
   - `openid` and `https://www.googleapis.com/auth/userinfo.email` (non-sensitive: linking
     uses them only to record which address the account belongs to)

   No directory scope is used.
4. **Publish the app: set the publishing status to *In production*.** While it is in *Testing*,
   Google expires refresh tokens after 7 days and you would have to re-link every account weekly.
5. **Create the client**: *Credentials → Create credentials → OAuth client ID → Application type
   **Desktop app***. Download the JSON. It is a secret: keep it out of the repository (the
   `.gitignore` already excludes `client_secret*.json`).

Give the server the client either as `GOOGLE_CLIENT_SECRETS_FILE=/path/to/client_secret.json`
(the downloaded file with its `installed` key; a *Web application* client is rejected with a
clear message), or as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (each also available as
`…_FILE`). Setting both ways is an error.

**The "unverified app" warning.** An app in production that has not gone through Google's
verification shows a "Google hasn't verified this app" screen when you link an account: choose
*Advanced → Go to … (unsafe)*. For personal use that is fine: the `contacts` scope is
*sensitive*, not *restricted*, and an unverified app can have up to 100 users. Verification is
only needed to remove the warning or to go beyond 100 users.

**Google Workspace accounts.** A Workspace admin may block third-party apps. If linking fails
with an "access blocked" or admin-policy error, an admin has to allow the client in the Admin
console (*Security → Access and data control → API controls → Manage third-party app access*,
by client ID). If every account you want to link belongs to one Workspace organization, you can
instead choose the **Internal** audience on the consent screen: no publishing, no warning, and
no 7-day token expiry, but only that organization's accounts can sign in.

## 3. Linking accounts

Accounts are linked with the `contacts-mcp auth …` command. It works headless, with no tunnel. The alias is yours to pick: lowercase letters, digits and `-`,
up to 32 characters (`personal`, `work`).

Each linked account is a file `accounts/<alias>.json` in the data directory
(`CONTACTS_MCP_DATA_DIR`), mode 0600, holding the refresh token, the account's e-mail, the
granted scopes and its read-only flag.

### Paste flow (default)

```sh
contacts-mcp auth add personal
```

1. The command prints a Google sign-in URL. Open it in any browser, on any machine.
2. Sign in, accept the "unverified app" screen if shown, and grant every permission asked.
3. Google redirects to `http://127.0.0.1:<port>/?state=…&code=…`. The page fails to load: that is
   expected. Copy the **entire URL** from the address bar.
4. Paste it into the terminal. The command checks `state`, exchanges the code (with PKCE), reads
   the account's e-mail and saves the file.

### Listening flow

```sh
contacts-mcp auth add personal --listen 8765
```

The command serves the loopback redirect itself, so the browser completes the flow and nothing
needs pasting. Use it on your own machine, or on a server reached through a tunnel:

```sh
ssh -L 8765:127.0.0.1:8765 you@server      # on your machine
contacts-mcp auth add personal --listen 8765   # in that ssh session; open the printed URL locally
```

Add `--read-only` to link the account as read-only from the start.

### Locally

```sh
mise exec -- uv run contacts-mcp auth add personal
```

The data directory defaults to `~/.local/share/contacts-mcp` (`$XDG_DATA_HOME/contacts-mcp`).

### Managing accounts

```sh
contacts-mcp auth list                          # aliases, e-mails, read-only, status
contacts-mcp auth set-read-only personal on     # or off
contacts-mcp auth remove personal               # also revokes the token at Google, best effort
```

If Google revokes a refresh token (you removed the app's access in your Google account, or the
token went unused for too long), the tools say so and name the command to run: `auth add <alias>`
again for that alias.

## Configuration

Everything is configured with environment variables, validated at startup (errors name the
variable). Each secret can be given as `NAME` or as `NAME_FILE` (a path, for Docker/Podman
secrets), not both.

| Variable | Default | Notes |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (`_FILE`) | | The Desktop-app client. Or: |
| `GOOGLE_CLIENT_SECRETS_FILE` | | the downloaded Desktop-app JSON |
| `CONTACTS_MCP_DATA_DIR` | `$XDG_DATA_HOME/contacts-mcp` (`~/.local/share/…`) | account files |
| `CONTACTS_MCP_LOG_LEVEL` | `INFO` | JSON lines on stderr |

## Development

Tools: [uv](https://docs.astral.sh/uv/) and Python 3.14. [mise](https://mise.jdx.dev/) pins uv
and the Python minor version, and defines the tasks; plain `uv` works the same.

| mise | plain uv |
| --- | --- |
| `mise run setup` | `uv sync --locked` |
| `mise run check` | `uv run ruff check . && uv run ruff format --check . && uv run mypy` |
| `mise run fix` | `uv run ruff check --fix . && uv run ruff format .` |
| `mise run test` | `uv run pytest` |

CI (GitHub Actions, [`check.yml`](.github/workflows/check.yml)) runs the same tasks as one
check per job on every pull request, stacked ones included, and on pushes to `main`.

The People API is mocked at the HTTP layer in the tests, so they need no network and no account.

[`docs/DESIGN.md`](docs/DESIGN.md) describes the module layout and how the pieces fit.

## License

[GPL-3.0-or-later](LICENSE).
