# ContactsMCP design

A self-hostable MCP server exposing Google Contacts (People API) for several Google accounts,
over stdio (local) or streamable HTTP behind OAuth (deployed). This note describes how the code
is organized and the decisions that are not obvious from reading it. User-facing behaviour and
configuration are in the [README](../README.md).

## Module layout (`src/contacts_mcp/`)

| Module | Role |
| --- | --- |
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

## Configuration

Environment variables only, validated at startup with errors that name the variable; each
secret can also come from `<NAME>_FILE`. The variables are listed in the
[README](../README.md#configuration).
