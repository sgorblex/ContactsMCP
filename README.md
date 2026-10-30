# ContactsMCP

A self-hostable [MCP](https://modelcontextprotocol.io/) server for Google Contacts (the People
API), with several Google accounts side by side. Connect it to claude.ai (custom connector),
Claude Code or any other MCP client, and let the assistant search, read, create and edit your
contacts.

- **Multiple accounts**, each linked once from the command line and addressed by an alias.
- **Careful edits**: `update_contact` changes only what you ask. Existing phone numbers, e-mails
  and the like are never silently dropped.
- **Safe to expose**: a per-account read-only flag, a tool allowlist, an audit log, and (over
  HTTP) OAuth against your own identity provider plus a user allowlist.
- **Stdio for local use, streamable HTTP for deployment** behind your own reverse proxy.
- A small container image.

Contents: [1. What it does](#1-what-it-does) · [2. Google Cloud setup](#2-google-cloud-setup) ·
[3. Identity provider setup](#3-identity-provider-setup) ·
[4. Linking accounts](#4-linking-accounts) · [5. Running it](#5-running-it) ·
[6. Connecting a client](#6-connecting-a-client) · [7. What to back up](#7-what-to-back-up) ·
[Configuration](#configuration) · [Tool exposure](#tool-exposure) ·
[Read-only accounts](#read-only-accounts) · [Security](#security) · [Development](#development) ·
[License](#license)

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
| `list_groups` | read | Contact groups (labels), user and system, with member counts. |
| `list_other_contacts` | read | The "Other contacts" Google collects automatically (people you e-mailed). |
| `search_other_contacts` | read | Search "Other contacts". |
| `create_contact` | write | Create a contact: names, nicknames, e-mails, phones, organizations, addresses, birthday, notes, URLs, relations, events, custom fields, labels. |
| `update_contact` | destructive | Edit a contact. Only what you ask changes: existing values are removed only by an explicit `remove`, `replace_all` or `clear`, or replaced by a new `birthday` or `biography` (which is why it is marked destructive). Multi-valued fields take explicit `add`, `remove`, `replace_all` and `set_primary`; the result lists before and after for each touched field. |
| `copy_other_contact_to_contacts` | write | Promote an "Other contact" to a real contact. |
| `batch_create_contacts` | write | Create up to 200 contacts in one call; any Google refuses are listed in `failures`. |
| `batch_update_contacts` | destructive | Update up to 200 contacts with the same rules as `update_contact`; any Google refuses are listed in `failures`. |
| `create_group` | write | Create a label. |
| `rename_group` | write | Rename a user label (idempotent). |
| `add_to_group` | write | Add contacts to a label (idempotent). |
| `remove_from_group` | destructive | Remove contacts from a label (the contacts stay); reports those Google did not find or could not remove. |
| `delete_contact` | destructive | Delete a contact; returns what was deleted. |
| `delete_group` | destructive | Delete a user label. Its contacts stay unless `delete_contacts` is true. |

Labels can be given by resource name (`contactGroups/…`) or exact name.

Every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint`) and refuses arguments it does not know, naming them. Write tools also refuse on
[read-only accounts](#read-only-accounts).

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

## 3. Identity provider setup

Only for the HTTP deployment. claude.ai custom connectors cannot send a static token, so the
server authenticates MCP clients with OAuth and delegates the actual sign-in to an OpenID
Connect (OIDC) provider **you** choose. Over stdio there is no authentication and none of this
applies.

What any OIDC provider needs:

- A **confidential client** (it has a client secret) for this server.
- The redirect URI **`<base_url>/auth/callback`**, for example
  `https://contacts.example.com/auth/callback`, matching `CONTACTS_MCP_BASE_URL` exactly.
- The scopes **`openid email offline_access`** (the server requests these regardless of what the
  MCP client asks for), with **refresh tokens enabled** for the client. Some providers need
  `offline_access` switched on explicitly.
- A discovery document (`…/.well-known/openid-configuration`), whose URL goes into
  `CONTACTS_MCP_OIDC_CONFIG_URL`, plus the client ID and secret.
- A way to identify you in `CONTACTS_MCP_ALLOWED_USERS`: your e-mail address, matched only when
  the provider marks it verified (the `email_verified` claim), or your `sub` value.

### authentik

1. *Applications → Providers → Create → OAuth2/OpenID Provider.* Client type **Confidential**.
   Note the client ID and secret.
2. **Redirect URIs/Origins**: strict match `https://contacts.example.com/auth/callback`.
3. **Scopes**: select the default `openid` and `email` mappings and the
   **`offline_access`** one (*authentik default OAuth Mapping: OpenID 'offline_access'*).
4. **Signing Key**: pick a certificate/key pair, so tokens are signed with RS256 rather than with
   the client secret.
5. Leave refresh token validity at its default or set it as you like.
6. *Applications → Applications → Create*, choose the provider and a slug, say `contacts-mcp`,
   and restrict who may open it with a policy or group binding if you want.
7. Use the discovery URL of that application:

```sh
CONTACTS_MCP_OIDC_CONFIG_URL=https://auth.example.com/application/o/contacts-mcp/.well-known/openid-configuration
CONTACTS_MCP_OIDC_CLIENT_ID=<client id>
CONTACTS_MCP_OIDC_CLIENT_SECRET=<client secret>
CONTACTS_MCP_ALLOWED_USERS=<your authentik sub>
```

**Allow by `sub`, not e-mail.** authentik's default `email` scope mapping always sends
`"email_verified": false`, so an e-mail entry in `CONTACTS_MCP_ALLOWED_USERS` never matches.
List your `sub` instead: with the provider's default subject mode it is the hashed user ID, and
a refused sign-in logs it (`contacts_mcp.oidc` logger, "sign-in refused"). Alternatively, use a
custom email mapping that returns `email_verified: true` for addresses authentik has verified.

### Google as the identity provider

You need a second OAuth client for the sign-in, separate from the Desktop client of section 2.

1. In the same Cloud project (or another): *Credentials → Create credentials → OAuth client ID →
   Application type **Web application***. Under *Authorized redirect URIs* add
   `https://contacts.example.com/auth/callback`.
2. Use these settings:

```sh
CONTACTS_MCP_OIDC_CONFIG_URL=https://accounts.google.com/.well-known/openid-configuration
CONTACTS_MCP_OIDC_CLIENT_ID=<web client id>
CONTACTS_MCP_OIDC_CLIENT_SECRET=<web client secret>
CONTACTS_MCP_OIDC_UPSTREAM_SCOPES=openid email
CONTACTS_MCP_OIDC_EXTRA_AUTHORIZE_PARAMS=access_type=offline&prompt=consent
CONTACTS_MCP_OIDC_VERIFY_ID_TOKEN=true
CONTACTS_MCP_ALLOWED_USERS=you@example.com
```

Why the three extra lines: Google rejects `offline_access` as a scope and issues a refresh token
only for `access_type=offline` (with `prompt=consent` so it does so on every sign-in), and its
access tokens are opaque, so the server verifies the signed ID token instead. (This is about
signing in to the MCP server; the Google accounts you link for contacts keep their own refresh
tokens either way.)

**`CONTACTS_MCP_ALLOWED_USERS` is essential with a public provider.** Google lets anyone with a
Google account complete the sign-in. Without the server-side allowlist, any of them could
connect to your server and read your contacts. HTTP mode therefore refuses to start without it.
With a private provider the allowlist is a second line of defence.

## 4. Linking accounts

Accounts are linked with the `contacts-mcp auth …` command. It works headless, with no tunnel,
and while the server is running. The alias is yours to pick: lowercase letters, digits and `-`,
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

### In a container

```sh
podman run --rm -it -v contacts-data:/data --env-file .env \
  ghcr.io/<owner>/contacts-mcp contacts-mcp auth add personal
```

Use the same data volume as the server, and the same Google client settings (the `.env` file,
or `-e GOOGLE_CLIENT_SECRETS_FILE=… -v ./client_secret.json:…:ro`). Use the paste flow here; the
`--listen` flow binds the loopback of the container, so it needs `--network host`.

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

A running server reads the account files at startup and again on **`SIGHUP`**: after adding,
removing or changing an account, run `podman kill -s HUP contacts-mcp` (or `docker kill -s HUP`,
or `systemctl --user kill -s HUP contacts-mcp` for a Quadlet unit), or restart it. A file that
cannot be read never crashes the server; it is reported by the `accounts` tool.

If Google revokes a refresh token (you removed the app's access in your Google account, or the
token went unused for too long), the tools say so and name the command to run: `auth add <alias>`
again for that alias.

## 5. Running it

### Locally over stdio

No authentication, no HTTP: the client starts the server as a subprocess. Link an account first
(section 4) and have `GOOGLE_CLIENT_SECRETS_FILE` (or the ID/secret pair) in the environment.

Claude Code:

```sh
claude mcp add contacts \
  -e GOOGLE_CLIENT_SECRETS_FILE=/path/to/client_secret.json \
  -- uv run --directory /path/to/ContactsMCP contacts-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "contacts": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ContactsMCP", "contacts-mcp"],
      "env": {
        "GOOGLE_CLIENT_SECRETS_FILE": "/path/to/client_secret.json"
      }
    }
  }
}
```

To look at the tools without a chat client, use the MCP Inspector:

```sh
npx @modelcontextprotocol/inspector mise exec -- uv run contacts-mcp
```

or `mise exec -- uv run python scripts/list_tools_stdio.py`, which prints the tool names and annotations.

### Self-hosted over HTTP

With `CONTACTS_MCP_TRANSPORT=http` (the image's default) the server speaks streamable HTTP on
port 8000 at `/mcp`, and refuses to start without the identity provider settings (section 3) and
the user allowlist: there is no insecure mode. Copy [`.env.example`](.env.example) to `.env` (no
quotes around values: `podman --env-file` keeps them as part of the value) and fill in the
Google client, the base URL, the identity provider and the allowlist.

Podman or Docker:

```sh
podman run -d --name contacts-mcp --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v contacts-data:/data -v contacts-oauth:/oauth \
  --env-file .env \
  ghcr.io/<owner>/contacts-mcp:latest
```

`mise run build-container` builds the image locally as `contacts-mcp:dev`; the examples use the
name it is published under, `ghcr.io/<owner>/contacts-mcp`.

Other options, all in [`deploy/`](deploy/):

- [`compose.yml`](deploy/compose.yml): Docker Compose / podman-compose, with secrets read from
  files (`…_FILE` variables) and the one-shot account-linking command. The server runs as uid
  1000, so the secret files must be readable by it (rootless Podman: `podman unshare chown
  1000:1000 secrets/*`).
- [`contacts-mcp.container`](deploy/contacts-mcp.container): a Podman Quadlet unit for systemd,
  with Podman secrets and the auto-update label.
- [`reverse-proxy.md`](deploy/reverse-proxy.md): TLS is terminated at your reverse proxy, which
  forwards to port 8000. Caddy and nginx snippets, the streaming settings, and the
  `CONTACTS_MCP_BASE_URL` / `CONTACTS_MCP_TRUST_FORWARDED_HEADERS` /
  `CONTACTS_MCP_FORWARDED_ALLOW_IPS` settings that must agree with it.

The examples use Podman/Docker secrets and named volumes, but nothing depends on them. A plain
env file (mode 0600) with every secret in it and bind-mounted host directories work just as well:
give the Google client as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` instead of the JSON file,
and make the `/data` and `/oauth` directories owned by uid 1000 with mode 0700 (with rootless
Podman, through `podman unshare chown`). Back up the env file together with the account files.

`/healthz` answers without authentication: the process is up and the account files are
readable (no Google call; `?deep=1` also tries a token refresh per account). It reports counts
only; the aliases of accounts needing attention go to the log. The image's `HEALTHCHECK` uses
it.

Then link accounts (section 4) and send the server a `SIGHUP`.

## 6. Connecting a client

**claude.ai**: *Settings → Connectors → Add custom connector*, name it, and enter
`https://contacts.example.com/mcp`. Claude sends you to your identity provider to sign in, and
back.

**Claude Code**:

```sh
claude mcp add --transport http contacts https://contacts.example.com/mcp
```

Then run `/mcp` inside Claude Code and choose *Authenticate*: the OAuth login opens in your
browser.

Any other MCP client that supports OAuth 2.1 with dynamic client registration works too, provided
its redirect URI is allowed (`CONTACTS_MCP_ALLOWED_REDIRECT_URIS`; by default claude.ai, claude.com
and Claude Code's loopback callback). Whoever signs in must be in `CONTACTS_MCP_ALLOWED_USERS`.

## 7. What to back up

- **`/data`** (the account files, `CONTACTS_MCP_DATA_DIR`): the refresh tokens. Losing them means
  linking each account again; they are small and written atomically, so they can be copied while
  the server runs. Treat the backup as secret.
- **`/oauth`** (`FASTMCP_HOME`) is **disposable**: OAuth client registrations and upstream
  tokens. Losing it only makes MCP clients sign in again.
- Your configuration (`.env`, the secrets and the Google client JSON) is yours to keep; the
  Google client can also be re-downloaded or recreated in the console.

## Configuration

Everything is configured with environment variables, validated at startup (errors name the
variable). Each secret can be given as `NAME` or as `NAME_FILE` (a path, for Docker/Podman
secrets), not both. [`.env.example`](.env.example) is the commented template.

| Variable | Default | Notes |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (`_FILE`) | | The Desktop-app client. Or: |
| `GOOGLE_CLIENT_SECRETS_FILE` | | the downloaded Desktop-app JSON |
| `CONTACTS_MCP_TRANSPORT` | `stdio` (`http` in the image) | `stdio` or `http` |
| `CONTACTS_MCP_DATA_DIR` | `$XDG_DATA_HOME/contacts-mcp` (`~/.local/share/…`; `/data` in the image) | account files |
| `CONTACTS_MCP_SERVER_NAME` | `contacts` | |
| `CONTACTS_MCP_EXPOSED_TOOLS` | `all` | see [Tool exposure](#tool-exposure) |
| `CONTACTS_MCP_LOG_LEVEL` | `INFO` | JSON lines on stderr |
| `CONTACTS_MCP_HOST`, `CONTACTS_MCP_PORT` | `0.0.0.0`, `8000` | HTTP |
| `CONTACTS_MCP_BASE_URL` | required (HTTP) | public URL, e.g. `https://contacts.example.com` |
| `CONTACTS_MCP_OIDC_CONFIG_URL` | required (HTTP) | OIDC discovery URL |
| `CONTACTS_MCP_OIDC_CLIENT_ID`, `CONTACTS_MCP_OIDC_CLIENT_SECRET` (`_FILE`) | required (HTTP) | |
| `CONTACTS_MCP_JWT_SIGNING_KEY` (`_FILE`) | required (HTTP) | at least 32 characters |
| `CONTACTS_MCP_ALLOWED_USERS` | required (HTTP) | e-mails and/or `sub` values, comma-separated |
| `CONTACTS_MCP_ALLOWED_REDIRECT_URIS` | claude.ai, claude.com, loopback | comma-separated, `:*` is a port wildcard |
| `CONTACTS_MCP_OIDC_UPSTREAM_SCOPES` | `openid email offline_access` | |
| `CONTACTS_MCP_OIDC_EXTRA_AUTHORIZE_PARAMS` | | extra provider parameters, `key=value&key=value` (not `scope`) |
| `CONTACTS_MCP_OIDC_VERIFY_ID_TOKEN` | `false` | verify the ID token instead of the access token (Google) |
| `CONTACTS_MCP_TRUST_FORWARDED_HEADERS` | `false` | trust `X-Forwarded-For` / `-Proto` |
| `CONTACTS_MCP_FORWARDED_ALLOW_IPS` | `127.0.0.1` | proxies trusted for them |
| `FASTMCP_HOME` | `~/.local/share/fastmcp` (`/oauth` in the image) | OAuth state |

## Tool exposure

`CONTACTS_MCP_EXPOSED_TOOLS` decides which tools the server offers at all, so a deployment that
should only read cannot be talked into writing:

```sh
CONTACTS_MCP_EXPOSED_TOOLS=all                                  # default
CONTACTS_MCP_EXPOSED_TOOLS=read-only                            # only read tools
CONTACTS_MCP_EXPOSED_TOOLS=read-only+create_contact,add_to_group   # read tools plus these
CONTACTS_MCP_EXPOSED_TOOLS=all-delete_contact,delete_group      # everything except these
```

An unknown tool name stops the server at startup, and the startup log lists the hidden tools.

## Read-only accounts

An account can be marked read-only, independently of which tools are offered:

```sh
contacts-mcp auth set-read-only work on
```

Write tools then refuse on that account (and the refusal is audit-logged), while reads keep
working.

## Security

See [SECURITY.md](SECURITY.md) for the threat model and how to report a vulnerability. In short:
HTTP mode always requires OAuth and a user allowlist; refresh tokens live in the data directory
with mode 0600; contact text is treated as untrusted; every write attempt is logged as an audit
line.

## Development

Tools: [uv](https://docs.astral.sh/uv/) and Python 3.14. [mise](https://mise.jdx.dev/) pins uv
and the Python minor version, and defines the tasks; plain `uv` works the same.

| mise | plain uv |
| --- | --- |
| `mise run setup` | `uv sync --locked` |
| `mise run dev` | `CONTACTS_MCP_DATA_DIR=.local/data uv run contacts-mcp` |
| `mise run check` | `uv run ruff check . && uv run ruff format --check . && uv run mypy` |
| `mise run fix` | `uv run ruff check --fix . && uv run ruff format .` |
| `mise run test` | `uv run pytest` |
| `mise run build-container` | `podman build --format docker -f Containerfile -t contacts-mcp:dev .` |

CI (GitHub Actions, [`check.yml`](.github/workflows/check.yml)) runs the same tasks as one
check per job on every pull request, stacked ones included, and on pushes to `main`, plus a
container build.

The People API is mocked at the HTTP layer in the tests, so they need no network and no account.

[`scripts/smoke_test.py`](scripts/smoke_test.py) is an opt-in end-to-end run against a **real**
account: it creates a throwaway contact and label and deletes them again. It never runs in CI:

```sh
CONTACTS_MCP_SMOKE=1 mise exec -- uv run python scripts/smoke_test.py personal
```

[`docs/DESIGN.md`](docs/DESIGN.md) describes the module layout and how the pieces fit.

## License

[GPL-3.0-or-later](LICENSE).
