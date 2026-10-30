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

## Configuration

Environment variables only, validated at startup with errors that name the variable; each
secret can also come from `<NAME>_FILE`. The variables are listed in the
[README](../README.md#configuration).
