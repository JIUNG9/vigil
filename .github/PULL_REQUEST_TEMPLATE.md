<!--
Thanks for sending a PR! Fill out the sections below so the review goes fast.

If this PR is a draft or work-in-progress, please open it as a Draft PR
(the dropdown next to "Create pull request"). That signals "looking for early
feedback, don't review the whole thing yet."
-->

## Summary

<!-- 2-3 sentences. What does this PR do, at a high level? -->

## Why

<!-- What problem does this solve? Link to the issue or discussion if there is one.
Closes #NNN  -->

## How

<!-- Brief overview of the implementation approach. What did you change and why
that approach rather than an alternative? -->

## Test plan

<!-- Concrete steps the reviewer can run to verify this works.
Example:
1. `pytest tests/rag/test_rerank.py -v`
2. Start Ollama locally
3. Run `vigil ask "ArgoCD sync failure"` and verify top-3 includes the runbook
-->

- [ ] Unit tests added / updated and pass locally
- [ ] `ruff check .` clean
- [ ] `ruff format --check .` clean
- [ ] `mypy src` clean
- [ ] Manual smoke test done (describe above)

## Checklist

- [ ] `CHANGELOG.md` has an entry under `## Unreleased`
- [ ] No personal data, credentials, internal hostnames, or `.env` files committed
- [ ] No drive-by formatting changes to unrelated files (keep the diff focused)
- [ ] Public APIs have type hints + a short docstring
- [ ] Documentation updated where behavior changed (README, docs/)

## Breaking changes

<!-- If this PR changes a public API, CLI flag, file format, or config schema:
describe the breakage and provide a migration path. If no breaking changes,
write "none". -->

none

## Related PRs / discussions

<!-- Optional: link to related discussions, prior PRs, or follow-up work. -->
