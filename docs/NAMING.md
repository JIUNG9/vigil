# Naming convention

A team's naming convention is one of those things that nobody pays for
until they've already paid. Pile up enough repos shaped like
`foo-bar-server-api-v2`, `baz-svc`, `thing-front-prod`, and the day-one
cost of "is this an app, an infra repo, or a library?" rolls forward
forever — every onboarder, every grep, every audit, every stale-link
hunt. `vigil naming` ships a configurable validator so you can pay
the cost once.

## Why a naming convention

Three concrete arguments, in priority order.

**Cognitive load on the reader.** Five seconds per repo across a
50-repo org is four minutes. Per engineer. Per day. For the lifetime of
the org. A consistent shape — prefix tells you the org, category tells
you the kind, domain tells you the account, type tells you what to
build with — collapses that to zero.

**Blast radius and ownership.** When an alert names `acme-infra-platform-secrets-tfmod`,
you already know which AWS account is involved (the platform domain),
who's on call (the platform team), and that touching it changes
Terraform module consumers (because the type is `tfmod`, not `tfstate`).
A name that doesn't decode this way leaves the on-call grepping for the
runbook.

**Audit trail and search.** Grep is a database query language. Stable
prefixes and types let you write `grep -l '^acme-infra-' INDEX | xargs ...`
across the whole org and trust the result. That gets you answers like
"every IaC repo we own" or "every iOS app under the data domain" in
one shell line — no Confluence page required.

## The structural pattern

```
{prefix}-{category}-{domain}-{service}[-{submodule}]-{type}
```

| Slot      | Position | Required | Meaning                                                |
|-----------|---------:|----------|--------------------------------------------------------|
| prefix    | 1        | yes      | Org / brand identifier. `acme` in the shipped example. |
| category  | 2        | yes      | The repo's role: `app`, `infra`, `lib`, `ops`, `doc`, `poc`. |
| domain    | 3        | yes      | The account / sub-org boundary. Often a 2–8 letter code (`core`, `data`, `platform`, `shared`). |
| service   | 4        | yes      | A product name from a controlled dictionary.           |
| submodule | 5        | no       | Exceptional. Used only when one service has multiple repos of the same type. |
| type      | last     | yes      | The artifact shape: `api`, `web`, `sdk`, `tfmod`, etc. |

Five tokens is the common case. Six tokens is reserved for the
submodule edge case. Anything outside `5..6` fails the count check.

## The hard rules

The validator enforces the following. Each is a hard FAIL except where
noted. The reference Korean shell validator at `check-repo-name.sh`
inside the source repo encodes exactly these rules — `vigil
naming check` is a port to Python with a configurable vocabulary.

1. **Charset** — `[a-z0-9-]` only. No UPPER, no `_`, no spaces, no
   `--`, no leading/trailing `-`, no token starting with a digit.
2. **Token count** — within `[constraints].min_tokens..max_tokens`.
   Default `5..6`.
3. **Type token last** — `tokens[-1]` must be in
   `[token.type].values`. Type-last is structural; a type-first or
   type-middle name silently breaks downstream regex tooling.
4. **Service dictionary** — when `[token.service].strict = true`
   (default), the service token must be in the configured list.
   Adding a new service is a PR to that list. That's the workflow
   — see "PR-to-add-service" below.
5. **No duplicate tokens** — the same token can't appear at two
   different positions. `acme-app-shared-billing-app-api` fails because
   `app` is at the category and (would-be) submodule slot.
6. **Length cap** — over `[constraints].max_length` is a WARN, not
   FAIL. Length is a heuristic; the rest are structural.
7. **Exceptions** — names listed in `[exceptions].allow` pass
   unchecked. Use sparingly, e.g. `acme-docs` for the legacy short-form
   repo that everyone already remembers.

## The submodule philosophy

The submodule slot exists because reality has edge cases, and ignoring
them produces worse names than admitting them.

It is **exceptional**. Default to no submodule. Two simultaneous
conditions justify one:

1. The same service has **multiple repos of the same type token** —
   typical examples are `admin` vs `partner` web frontends, or `b2c`
   vs `b2b` apis.
2. **Monorepo consolidation isn't viable** — separate build pipelines,
   separate ownership, separate security boundaries.

If the candidate "submodule" is really a separate product, register it
as a service in the dictionary instead. The submodule axis is for
"same service, different shape," not "loosely related products."

The validator forbids the submodule from duplicating any **category**
or **type** token. `acme-app-data-pricing-app-api` fails because the
submodule duplicates the category. Pick a service-meaningful name
(`pricing-engine` is a service, `customer` is a submodule).

## Configuring for your team

Drop a `.vigil-naming.toml` at your brain root:

```toml
[locale]
language = "en"

[token.prefix]
values = ["acme"]
strict = true

[token.category]
values = ["app", "infra", "lib", "ops", "doc", "poc"]
strict = true

[token.domain]
values = ["core", "data", "platform", "shared"]
strict = true

[token.service]
values = ["billing", "identity", "pricing"]
strict = true

[token.submodule]
recommend = ["admin", "partner", "consumer", "merchant", "b2b", "b2c"]
forbid_duplicating = ["category", "type"]

[token.type]
values = [
  "agw", "api", "worker", "web", "webview",
  "ios", "android", "win", "did",
  "sdk", "schema", "tfstate", "tfmod", "k8s", "docs",
]
strict = true

[constraints]
min_tokens = 5
max_tokens = 6
max_length = 50

[exceptions]
allow = []
```

Four starter templates ship with `vigil`:

| Template          | Use case                                                      |
|-------------------|---------------------------------------------------------------|
| `nexus-style`     | Multi-domain org, full taxonomy, exceptional submodule slot.  |
| `small-team`      | Three categories, one shared domain, no submodule.            |
| `monorepo-only`   | Single repo; convention applies to top-level dirs inside it.  |
| `strict-iac`      | IaC galaxy. Categories `{infra, ops}`, types `{tfmod, tfstate, k8s, docs}`. |

Bootstrap with `vigil naming init --template nexus-style`. Print the
effective convention with `vigil naming list`. Test a candidate name
with `vigil naming check acme-infra-core-billing-tfmod`.

## The PR-to-add-service workflow

The service dictionary is a feature, not friction. When a new service
appears, the workflow is:

1. PR the new entry to `[token.service].values` in
   `.vigil-naming.toml`. The PR description names the new service,
   its domain, and the type(s) it will produce.
2. A docs reviewer (or whoever owns naming for the org) approves.
3. The new service token is now legal everywhere.

That review step is doing real work: it forces the team to confirm the
new product actually belongs in the org's namespace, not in a
contractor's, and that the chosen token doesn't collide with anything
already in the dictionary.

## Migrating from an unmanaged namespace

You don't get to name 50 repos at once. The migration plan looks like:

1. **Adopt the convention for new repos immediately.** Run
   `vigil naming init` and require the check on every new repo's
   first PR.
2. **Add the legacy outliers to `[exceptions].allow`.** Names that
   pre-date the convention pass unchecked. The exception list is the
   migration backlog.
3. **Rename in waves, by team.** Renames are cheap when GitHub redirects
   the old URL, expensive when they break a hardcoded URL in a
   deployment script. Rename one team at a time and grep for the old
   name across the brain — `vigil ask "where is foo-bar referenced?"`
   is the right tool.
4. **Drop the exception line once renamed.** The exception list shrinks
   over time. When it's empty, you've finished migration.

If you're starting from scratch, the workflow inverts: write the
convention, then create repos. The dictionary entries become the change
log of "what services has this org built?"

## Locale support

Korean teams set `[locale] language = "ko"`. The validator's failure
messages are translated faithfully from the Korean reference shell
script:

```toml
[locale]
language = "ko"
```

The structural rules are identical. Only the message strings change.
English is the default; messages stay in English when the locale key
is absent or unrecognized.

## What this convention does NOT do

- **Branch names.** Git branch names are a separate convention. Most
  teams use `feature/`, `fix/`, `release/` prefixes — that lives
  somewhere else.
- **Kubernetes resource names.** `acme-infra-platform-secrets-tfmod`
  is the *repo* name; the namespace, deployment, or service inside is
  a separate naming question with different constraints (DNS-1123
  label, 63-character cap, etc.).
- **File names inside a repo.** A repo named `acme-app-core-billing-api`
  doesn't dictate that every Python file inside is named
  `acme_app_core_billing_*`. That's a code-style decision.
- **Cloud resource tags.** Tags often need separate keys for `domain`,
  `service`, and `env`; collapsing them into one string makes them
  hard to query in cost-explorer-shaped UIs.

The naming validator is the seatbelt for repo and service identifiers.
For everything else, write its own seatbelt.

## Integration with `vigil validate`

The naming check is OFF by default in `vigil validate` to avoid
surprising existing v0.6 users. Opt in two ways:

1. CLI flag: `vigil validate --include-naming`.
2. Per-repo TOML: in `.vigil/config.toml`,

   ```toml
   [validate]
   include_naming = true
   ```

When enabled, the check walks every directory directly under `docs/`,
`knowledge/`, and `.claude/skills/`, validating each name against the
loaded convention. The naming check is skipped silently if
`.vigil-naming.toml` is absent — naming is opt-in, not enforced.

See `docs/VALIDATE.md` for the full validate spec.

## See also

- `docs/VALIDATE.md` — the structural shape checker that the naming
  check plugs into.
- `docs/ADOPT.md` — mid-project migration. `vigil adopt` does not
  enforce naming today; the convention applies to repos, not files.
