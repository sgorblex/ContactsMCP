# ContactsMCP: the MCP server for Google Contacts, served as streamable HTTP on :8000/mcp
# behind an OAuth proxy. The same image runs the account-linking CLI (`contacts-mcp auth ...`).
FROM ghcr.io/astral-sh/uv:0.13.6-python3.14-trixie-slim@sha256:637619278855a77885a94312efa7decd8479ca6cec5f959e134cbab96582821e

LABEL org.opencontainers.image.title="ContactsMCP" \
      org.opencontainers.image.description="Self-hostable MCP server for Google Contacts (People API), with multiple accounts" \
      org.opencontainers.image.source="https://github.com/<owner>/ContactsMCP" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so this layer is cached until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

# Then the project itself, installed (not editable) so the `contacts-mcp` entry point exists.
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

RUN useradd --uid 1000 --create-home contacts \
    && install -d -o contacts -g contacts -m 0700 /data /oauth
USER contacts

ENV PATH=/app/.venv/bin:$PATH \
    CONTACTS_MCP_TRANSPORT=http \
    CONTACTS_MCP_DATA_DIR=/data \
    FASTMCP_HOME=/oauth \
    FASTMCP_SHOW_SERVER_BANNER=false \
    FASTMCP_CHECK_FOR_UPDATES=off
VOLUME /data /oauth
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('CONTACTS_MCP_PORT', '8000'), timeout=5)"]
CMD ["contacts-mcp"]
