# Test requirements

- Every PR runs the test suite in CI. Local: `<your-test-command>`.
- New code requires tests. Bug fixes require a regression test.
- Mocks: avoid mocking the database. Use a real test DB (Postgres in CI).
- Fixtures: live in `tests/fixtures/`. One sample-input dir per scenario.
- Coverage: aim for 80% on touched files. CI flags drops > 5%.
