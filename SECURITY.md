# Security

## Threat model

ContactsMCP gives an AI assistant read and write access to your Google Contacts. What it
defends, and against whom:

- **Who can reach the endpoint.** Over HTTP, every MCP request needs an OAuth access token
  issued after a sign-in at your OpenID Connect provider. On top of that the server keeps its
  own allowlist (`CONTACTS_MCP_ALLOWED_USERS`: verified e-mail addresses and/or `sub` values),
  checked on every request, because a public provider such as Google would otherwise let anyone
  with an account sign in. HTTP mode refuses to start without the OIDC settings and the
  allowlist. Apart from the OAuth flows (the MCP one and the settings page's sign-in),
  `/healthz` is the only unauthenticated endpoint, and it answers with counts only.
  Redirect URIs for MCP clients are limited to the configured list (claude.ai, claude.com and
  loopback by default), and the accepted `Host` is limited to the configured public host name and loopback names.
  TLS is the reverse proxy's job.
- **The settings page.** Off unless `CONTACTS_MCP_SETTINGS_PAGE` is set, and HTTP only. It signs
  you in at the same OIDC provider (authorization code with PKCE; the `state` also lives in a
  cookie, so the sign-in must finish in the browser that started it) and applies the same
  allowlist. Sessions are random tokens in a `__Host-` cookie (`Secure`, `HttpOnly`,
  `SameSite=Lax`), kept server-side only as their hash, ending after 30 idle minutes or 12
  hours. Every POST must carry the session's CSRF token and come from the server's own origin
  (`Origin`, else `Sec-Fetch-Site: same-origin`). Removing an account needs a sign-in from the
  last 10 minutes, otherwise a new login at the provider, checked by the ID token's
  `auth_time`. Pages have no scripts and a strict Content Security Policy. Each change is
  audited. MCP tools cannot change account settings.
- **Secrets at rest.** Google refresh tokens are stored in the data directory
  (`accounts/<alias>.json`, files 0600, directory 0700, written atomically), unencrypted:
  protect the volume and its backups like a password store. Access tokens are kept in memory
  only. The OAuth state under `FASTMCP_HOME` is encrypted with a key derived from
  `CONTACTS_MCP_JWT_SIGNING_KEY`. Secrets can be provided as files (`…_FILE`), and are never
  logged.
- **Prompt injection through contact text.** Names, notes and e-mail addresses in your
  contacts, and above all the *Other contacts* Google collects from people who e-mailed you, can
  be written by third parties and may contain instructions aimed at the model. The server says
  in its instructions and tool descriptions that this text is data, strips invisible and control
  characters from what it returns, and offers mitigations that do not depend on the model
  behaving: **read-only accounts** (write tools refuse), **tool exposure**
  (`CONTACTS_MCP_EXPOSED_TOOLS=read-only`, or removing `delete_*`) and the **audit log**.
  Use them: for an assistant that reads untrusted contact data, expose only what it needs.
- **Audit log.** Every write attempt, refused ones included, produces one JSON line on stderr
  with `"kind":"audit"`: the signed-in user, tool, account, resource name and the touched fields,
  with no field values beyond what identifies the contact.
- **Scope of Google access.** Only `contacts` and `contacts.other.readonly`, plus `openid email`
  when an account is linked, to record which address it belongs to. No directory, no mail, no
  other Google service.

## Out of scope

- Anyone with access to the host, the data volume or the container runtime: they can read the
  refresh tokens. Disk encryption, host hardening and backup security are yours.
- A compromised identity provider, or a user you put on the allowlist.
- A model that, within the tools you chose to expose, does something you did not intend. Use
  read-only accounts and tool exposure to bound it.
- Denial of service against a public endpoint: rate limiting belongs in the reverse proxy.
- Vulnerabilities in Google's services, fastmcp or other dependencies themselves (report those
  upstream); a ContactsMCP issue that makes them exploitable is in scope.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting: on the
repository, *Security → Advisories → Report a vulnerability*. Include what you found, how to
reproduce it and the version or image tag. You will get an answer as soon as possible, and a
fix and an advisory once the problem is confirmed.

Only the latest release is supported.
