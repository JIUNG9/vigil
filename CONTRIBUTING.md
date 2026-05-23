# Contributing to Vigil

Thank you for considering a contribution. Vigil is a small, focused project — a self-hosted DevSecOps command center built around a git-backed team-brain corpus. Contributions that keep the system simple, local-first, and predictable are the most welcome.

## Quick links

- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- Issue templates live in `.github/ISSUE_TEMPLATE/`

## Before you start

1. **Open a discussion first for non-trivial work.** New features or architectural changes should start as a GitHub Discussion or an issue with the `proposal` label. This avoids wasted effort.
2. **Check `CHANGELOG.md`** for what shipped in the current version, and the roadmap section for what's coming.
3. **Small PRs land faster.** Anything bigger than ~300 lines or touching more than 3 modules should be split.

## Development setup

Vigil requires Python ≥ 3.11.

```bash
git clone <your-fork-url>
cd vigil
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,rag]"
```

The optional `rag` extra pulls in `httpx` and `sqlite-vec` for the local RAG layer. Ollama itself is installed separately — see the README.

## Running tests

```bash
pytest                    # unit tests
pytest -m integration     # integration tests (require a running Qdrant + Ollama)
ruff check .              # linter
ruff format --check .     # formatter
mypy src                  # type checker
```

CI (`.github/workflows/ci.yml`) runs all of the above on every PR. Don't open a PR until the local suite passes.

## Coding style

- **Type hints everywhere.** `mypy --strict` is the target on `src/`. New code should be fully typed.
- **`ruff format` for whitespace.** No manual line-wrapping — let the tool decide.
- **Imports grouped:** stdlib → third-party → first-party (handled by ruff's `isort` rule).
- **Public functions get docstrings.** One-line summary, no novel-length docstrings. If a function's behavior needs a paragraph to explain, the function is too big.
- **Comments explain `why`, not `what`.** If you reach for a comment to describe what the next 5 lines do, rename the variables instead.

## Commit messages

Conventional Commits style. Examples:

```
feat(rag): add bge-reranker-v2-m3 cross-encoder
fix(indexer): handle empty diff in GitHub Compare API
docs(readme): clarify Ollama install steps
chore(deps): bump httpx to 0.27.2
```

Scopes we use today: `rag`, `indexer`, `dashboard`, `cli`, `mcp`, `slack`, `webhook`, `deps`, `ci`, `docs`, `readme`.

## Pull request checklist

- [ ] Tests pass locally (`pytest`, `ruff check`, `mypy src`)
- [ ] New behavior has a unit test
- [ ] `CHANGELOG.md` updated under the `## Unreleased` section
- [ ] No personal data, credentials, or internal hostnames leaked in fixtures
- [ ] PR description fills out the template (what / why / how)

The PR template (`.github/PULL_REQUEST_TEMPLATE.md`) has the boilerplate.

## Review cadence

Maintainer review usually within 3 business days. If the PR sits for a week without feedback, ping the maintainer in the PR — it almost certainly fell off the queue.

## Releases

We tag releases as `vX.Y.Z` on `main`. Tag push triggers `.github/workflows/release.yml` (GitHub Release with notes from `CHANGELOG.md`) and `.github/workflows/publish.yml` (PyPI publish via Trusted Publisher / OIDC, no API token in secrets).

Maintainers cut releases — contributors should not push tags.

## What we won't merge

To keep the surface small and the architecture stable:

- New chat tabs in the dashboard. v5 deliberately removed the chat tab; engineer-facing reasoning happens in the user's local Claude Code reading `~/.vigil/brain` directly.
- Cloud-only features (telemetry to external services, hosted SaaS modes). Vigil is local-first; the only outbound calls are to your own Ollama and Qdrant.
- Per-seat license enforcement.
- New paid storage backends. SQLite + Qdrant + git-backed markdown is the architecture.

If you have a strong case against any of these, open a Discussion first — we'll engage.

## Code of conduct

Be kind. Be specific. Critique code, not people. The full Code of Conduct is in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Reports go to the email listed there.

## License

By contributing, you agree your contribution will be licensed under the MIT License — the same license as the rest of the project. See `LICENSE`.
