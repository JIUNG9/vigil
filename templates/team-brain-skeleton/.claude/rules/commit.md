# Commit conventions

Conventional Commits. Format:

```
<type>(<scope>): <subject>

<body>

<footer>
```

Types: `feat`, `fix`, `chore`, `refactor`, `test`, `docs`, `ci`, `perf`, `style`.

Subject ≤ 72 chars, imperative mood ("add" not "added"), no trailing period.

Body explains WHY, not WHAT. The diff already shows what changed.

Footer carries `Refs:`, `Closes:`, breaking-change notices.
