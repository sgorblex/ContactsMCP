# Agent notes: ContactsMCP

The shared guide for work in this repository, by people and AI agents alike. **`CLAUDE.md`**
points here.

## Before you implement

- **[`README.md`](README.md)** is the user-facing documentation: tools, setup, configuration.
- **[`docs/DESIGN.md`](docs/DESIGN.md)** explains how the code is organized and the decisions
  that are not obvious from reading it (the People API client's retry policy, the update engine,
  the HTTP auth path).
- **[`SECURITY.md`](SECURITY.md)** is the threat model. Keep it true when you touch
  authentication, what is logged, or what the tools can reach.

Rules that hold everywhere:

- **Contact text is untrusted third-party data.** Every string a tool returns goes through
  `sanitize.clean()`, and tool descriptions say it is data, not instructions.
- **Edits never lose data they were not asked to touch.** Multi-valued fields change only
  through explicit operations, and validation finishes before anything is written.
- **No secrets in logs, errors or tool output:** tokens, client secrets, authorization codes,
  raw Google response bodies. Audit lines carry field *names*, never values.
- **No owner-specific values** (hosts, accounts, paths) anywhere: docs and examples use
  placeholders such as `contacts.example.com` and `ghcr.io/<owner>/contacts-mcp`.

## Shell commands (mise)

**mise** pins uv and the Python minor version (`mise.toml`) and defines the tasks. From the repository root:

- **`mise run <task>`**: `setup`, `dev`, `check`, `fix`, `test`, `build-container`.
- **`mise exec -- …`** for one-off invocations, e.g. `mise exec -- uv run pytest tests/test_update_engine.py`.

Do not invoke `uv`, `pytest`, `ruff` or `mypy` without `mise run` or `mise exec`. The README's
Development section lists the plain-uv equivalents for people without mise.

Run `mise run fix` after each pass; `mise run check` and `mise run test` before handing work
over; `mise run build-container` when the `Containerfile`, `.dockerignore`, `pyproject.toml` or
`uv.lock` change. Report work as done only once the checks have **actually run**, and say which
did not.

## Git and Graphite

- Branches are managed with **Graphite** (`gt`) and named **`<type>/<short_name>`**: lowercase
  `snake_case`, two to four words naming the change, `<type>` being the **Conventional Commit**
  type the branch squashes to (`feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`,
  `chore`, `style`, `revert`). Pass the name to `gt create` explicitly; otherwise `gt` derives
  one from the commit message.
- Never hand-create branches with the prefixes tools use: `release-please--`, `renovate/`,
  `graphite-base/`.
- **Conventional Commits** on every commit. The PR squash-merges into one commit on `main`, and
  its title is what Release Please reads for the changelog and the version bump. Put the **why**
  in the commit body: a reviewer reads the branch, not the conversation that produced it.
- **A branch ships as one unit.** If a later commit in an unmerged branch makes something from an
  earlier one obsolete, fix the earlier commit (`gt modify`) instead of adding a commit that
  undoes it. Merged history is never rewritten.
- Check the stack (`gt log short`) before starting and before adding branches, so new work
  stacks on the right parent.
- A stacked PR that cannot pass until the one below it merges stays a **draft**. Labels cannot
  pause CI here: on GitHub Actions a skipped job satisfies a required check.
- **Never run remote-mutating commands** (`git push`, `gt submit`, `gt push`, `gh pr create`,
  merging, commenting on or closing a PR) unless the user explicitly asks for it in the current
  request. Local commits and stack edits (`gt create`, `gt modify`, `gt restack`) need no asking.
- Feature-sized agent work goes in its own worktree under `.claude/worktrees/` (ignored). Ask
  before creating one from the main checkout. A harness-generated `worktree-…` branch is
  untracked by Graphite: start the real branch with `git switch -c <type>/<short_name>` and
  `gt track --parent <parent>`.

## Agent skills

Skills live in **`.agents/skills/<name>/`**; `.claude/skills/<name>` is a relative symlink to
it, never a second copy. **`review-stack`** is the self-review of a branch or a whole stack
before it is submitted.

## After you change the repository

Update the docs in the **same branch** as the change. Write as little as the rule needs: a row
or a link where a paragraph is not earning its place, and no comments that restate the code.

| You change… | Update… |
| --- | --- |
| A tool: added, removed, renamed, new arguments or annotations | README tool table (*What it does*); `docs/DESIGN.md` tools table; README *Tool exposure* examples if a name changes |
| Update-engine behaviour | `docs/DESIGN.md` *Update engine*; the README tool table if users see the difference |
| An environment variable | `config.py` validation (errors name the variable); README *Configuration*; `.env.example`; the `deploy/` examples when operators set it |
| Authentication, the allowlist, what is logged or exposed without auth | `SECURITY.md`; `docs/DESIGN.md` *HTTP mode* (and *Audit log* for the audit line) |
| A module added, moved or renamed; the People API client's retry or error policy; account storage | `docs/DESIGN.md` *Module layout*, *People API client* or *Accounts* |
| `contacts-mcp auth` commands or flags | README *Linking accounts*; the `deploy/` examples that run them |
| A mise task | README *Development* (with its plain-uv equivalent); the task list above; the verification table in `review-stack` |
| `Containerfile`, `.dockerignore`, `deploy/` | README *Self-hosted over HTTP*; `deploy/reverse-proxy.md` when the proxy setup changes |
| CI workflows or check names | README *Development*; after renaming a required check, update the ruleset (let the new name report once, then swap it in, then merge) |
| Versions or `CHANGELOG.md` | **Nothing by hand**: Release Please owns them |
| A `docs/TODO.md` item shipped or dropped | `docs/TODO.md` |
| A convention agreed with the user in conversation | This file or a skill, in the same change |
