# ContactsMCP design

A self-hostable MCP server exposing Google Contacts (People API) for several Google accounts,
over stdio (local) or streamable HTTP behind OAuth (deployed). This note describes how the code
is organized and the decisions that are not obvious from reading it. User-facing behaviour and
configuration are in the [README](../README.md).

## Module layout (`src/contacts_mcp/`)

| Module | Role |
| --- | --- |
| `cli.py` | `contacts-mcp` entry point: `contacts-mcp auth …` runs the account CLI |
| `config.py` | Environment → `Settings` / `HttpSettings` / `GoogleClientConfig`; `ConfigError` names the variable; `<NAME>_FILE` secrets |
| `errors.py` | `ContactsError(ToolError)` and subclasses: the only exceptions tools raise on purpose, with short messages that reach the model |
| `logs.py` | JSON-lines logging to stderr; `audit()` for write attempts |
| `identity.py` | `current_user()`: the verified e-mail (or `sub`) of the signed-in user; `local` over stdio |
| `sanitize.py` | `clean()`: strip Cc (except tab/newline) and Cf characters from returned strings |
| `fields.py` | Field masks (`PERSON_FIELDS`, `SUMMARY_FIELDS`, `WRITABLE_FIELDS`, …), scopes, API URL: one place, so reads and writes agree |
| `google/oauth.py` | Google OAuth URLs; `TokenProvider`: refresh via POST, access token in memory only, `invalid_grant` → `NeedsRelinkError` |
| `google/people.py` | `PeopleClient`: one method per endpoint, raw JSON in and out; retries, 401 refresh, error mapping |
| `models.py` | Pydantic tool inputs and outputs (all `extra="forbid"`), resource-name validators, `ListOps[T]` |
| `normalize.py` | People API JSON → `ContactSummary` / `Contact` / `OtherContact` / `Group` (read direction); values from the person's Google profile listed as `from_profile` |
| `accounts.py` | `AccountFileStore`: one JSON file per alias, written atomically, 0600 in a 0700 directory |
| `registry.py` | `AccountRegistry` / `Account`: routing by alias, per-account mutation lock, search warm-up, group-name cache, `reload()` |
| `auth_cli.py` | `auth add / list / remove / set-read-only` |

## People API client

The People API is a small REST surface, so it is called over `httpx` directly rather than through
`google-api-python-client`, and token refresh is one POST to Google's token endpoint rather than
`google-auth`. That keeps the image small and lets the tests mock
everything at the HTTP layer with `respx`.

- **Tokens.** `TokenProvider` keeps the access token in memory and refreshes it a minute before
  it expires. Concurrent callers share one refresh. A refresh that Google answers with
  `invalid_grant` marks the account as needing a re-link; later calls fail fast without asking
  Google again.
- **Retries.** A 401 forces one token refresh and a retry. Reads (`GET`) are retried with
  exponential backoff and jitter on 429, 5xx and transport errors, honouring `Retry-After`, a
  bounded number of times. Writes are retried only when Google certainly did not act on them
  (429, or no connection at all): after a 5xx or a read timeout a create may have happened, and a
  retry would duplicate it. Such a failure says the change may or may not have been applied.
- **Errors** map to short, actionable messages (`NotFoundError`, `EtagConflictError`,
  `PermissionDeniedError`, `RateLimitedError`, `GoogleApiError`), never the raw body or a token.
  An etag conflict is a 400 with status `FAILED_PRECONDITION` about the etag.
- **Field masks** (`personFields`, `readMask`) come from `fields.py`, so every read asks for the
  fields the writes know how to handle.
- **Batch writes** return Google's response for each contact, so a contact that failed inside a
  batch that succeeded is reported rather than lost.

## Accounts

- An account is `<data_dir>/accounts/<alias>.json`, alias `[a-z0-9][a-z0-9-]{0,31}`: the refresh
  token, the e-mail, the scopes granted and the `read_only` flag. Files are written atomically
  (temp file in the same directory, fsync, rename), so they back up safely while the server runs.
- `AccountRegistry.reload()` reads every file and never raises: an unreadable file becomes an
  `unreadable` account, reported rather than fatal. An account whose refresh token is unchanged
  keeps its runtime state (token cache, mutation lock, warm-up); a re-linked one starts fresh.
- `resolve(alias, write=…)` is the single routing point: an omitted alias works only with exactly
  one account; unknown, unreadable and needs-re-link accounts are errors naming what to do; writes
  on a `read_only` account are refused.
- The People API wants a user's mutations sent sequentially, so each account has a mutation lock.
  Searches need a warm-up request (empty query) once per account and process before the first
  real one; `Account.ensure_warm()` sends it.
- `auth add` runs OAuth with PKCE and `state` against a loopback redirect
  (`http://127.0.0.1:<port>/`), with `access_type=offline` and `prompt=consent` so Google always
  returns a refresh token. Google's out-of-band flow is gone, so the default flow has the user
  paste the redirect URL back; `--listen` serves the redirect for real. It also asks for
  `openid email` and takes the account's address from the ID token, since the `contacts` scope
  alone cannot read it from `people/me`.

## Configuration

Environment variables only, validated at startup with errors that name the variable; each
secret can also come from `<NAME>_FILE`. The variables are listed in the
[README](../README.md#configuration).
