# ContactsMCP

A self-hostable [MCP](https://modelcontextprotocol.io/) server for Google Contacts (the People
API), with several Google accounts side by side. Connect it to claude.ai (custom connector),
Claude Code or any other MCP client, and let the assistant search, read, create and edit your
contacts.

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

[`docs/DESIGN.md`](docs/DESIGN.md) describes the module layout and how the pieces fit.

## License

[GPL-3.0-or-later](LICENSE).
