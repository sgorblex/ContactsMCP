# Reverse proxy

ContactsMCP speaks plain HTTP on port 8000 and does not terminate TLS. Put it behind a reverse
proxy that does, and give the proxy the public name MCP clients will use (the examples use
`contacts.example.com`). claude.ai and most other clients require HTTPS.

## What the proxy must do

- Terminate TLS and forward everything under the host (`/mcp`, `/.well-known/…`, `/authorize`,
  `/token`, `/register`, `/auth/callback`, `/healthz`, and `/settings` with the settings page) to
  `127.0.0.1:8000`. Do not rewrite paths.
- Pass `Host` through (or set it to the public name) and send `X-Forwarded-Proto: https` and
  `X-Forwarded-For`.
- Not buffer responses and not cut idle connections early. The MCP endpoint is streamable HTTP:
  responses may be streamed, and clients keep requests open. Disable response buffering and
  use generous read timeouts (several minutes).

## Matching the server's configuration

The public URL is part of the OAuth setup, so these must agree:

| Where | Value |
| --- | --- |
| `CONTACTS_MCP_BASE_URL` | `https://contacts.example.com` (scheme and host as clients see them, no path) |
| Identity provider redirect URI | `https://contacts.example.com/auth/callback` (and `/settings/auth/callback` with the settings page) |
| Proxy site name | `contacts.example.com` |

The server only accepts requests whose `Host` is the host of `CONTACTS_MCP_BASE_URL` (DNS
rebinding protection), so a proxy that rewrites `Host` to something else gets rejected.

`X-Forwarded-*` headers are ignored unless you say the peer sending them is trusted:

```
CONTACTS_MCP_TRUST_FORWARDED_HEADERS=true
CONTACTS_MCP_FORWARDED_ALLOW_IPS=127.0.0.1
```

`CONTACTS_MCP_FORWARDED_ALLOW_IPS` lists the proxy addresses to trust (comma-separated; the
default is `127.0.0.1`). With the proxy in another container or host, put its address (or the
container network's) there. Do not set it to `*` unless the port is reachable only by the proxy.

Publish the container port on loopback (`127.0.0.1:8000:8000`) or on a private network, never
on a public interface: the proxy is the only thing that should reach it.

## Caddy

```caddyfile
contacts.example.com {
    reverse_proxy 127.0.0.1:8000 {
        # Flush streamed responses immediately.
        flush_interval -1
        transport http {
            read_timeout 10m
        }
    }
}
```

Caddy sets `X-Forwarded-Proto` and `X-Forwarded-Host` and keeps `Host` by default, and obtains
the certificate itself.

## nginx

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name contacts.example.com;

    ssl_certificate     /etc/letsencrypt/live/contacts.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/contacts.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;

        # Streamable HTTP: no buffering, long timeouts.
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

## Checking it

```sh
curl -i https://contacts.example.com/healthz          # 200, no authentication
curl -i https://contacts.example.com/mcp              # 401 with a WWW-Authenticate header
curl -s https://contacts.example.com/.well-known/oauth-protected-resource/mcp
```

The URLs in the last one come from `CONTACTS_MCP_BASE_URL`: if they show `http://` or an
internal host name, fix that setting.

If the identity provider is also behind a proxy, make sure the server can reach its discovery
URL from inside the container.
