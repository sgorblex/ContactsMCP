# ContactsMCP

A self-hostable [MCP](https://modelcontextprotocol.io) server for Google Contacts, with multiple
accounts.

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

## License

[GPL-3.0-or-later](LICENSE).
