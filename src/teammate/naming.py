"""Configurable repo / service naming convention.

A team's naming convention is one of those things that nobody pays for
until they've already paid: pile up enough repos with shapes like
``foo-bar-server-api-v2``, ``baz-svc``, ``thing-front-prod``, and the
day-one cost of "is this an app, an infra repo, or a library?" rolls
forward forever — every onboarder, every grep, every audit.

This module ships the structural pattern only. Every vocabulary token
is team-defined via ``.teammate-naming.toml``. The shipped pattern is::

    {prefix}-{category}-{domain}-{service}[-{submodule}]-{type}

with the following hard rules — these match the reference validator at
``check-repo-name.sh`` byte-for-byte:

  1. Charset ``[a-z0-9-]``. No UPPER, no ``_``, no spaces, no ``--``,
     no leading/trailing hyphen, no token that starts with a digit.
  2. Token count within ``[min_tokens, max_tokens]`` (default 5..6).
  3. ``tokens[0]`` is in ``[token.prefix].values``.
  4. ``tokens[1]`` (category) is in ``[token.category].values``.
  5. ``tokens[2]`` (domain) is in ``[token.domain].values``.
  6. ``tokens[3]`` (service) is in ``[token.service].values`` *if*
     ``[token.service].strict`` is true (default true).
  7. ``tokens[-1]`` (type) is in ``[token.type].values``.
  8. If a 6th token (submodule, position 5) is present, it must NOT
     duplicate any category or type token (forbid_duplicating).
  9. No duplicate token within a single name — same string at two
     different positions is forbidden.
 10. Length over ``max_length`` is a WARN, not a FAIL.
 11. Names listed in ``[exceptions].allow`` pass unchecked.

Locale: ``[locale].language`` toggles English vs Korean failure
messages. Both message tables are kept in lock-step — any change to
the validator must update both.

This module has no dependencies beyond ``tomllib`` (Python 3.11 stdlib).
"""

from __future__ import annotations

import enum
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------- shipped defaults (no proprietary tokens) ----------

# These defaults exist only so the dataclass has a sane starting state
# when a TOML file is missing keys. We do NOT ship a real prefix or
# service vocabulary — every team writes their own.

_DEFAULT_PREFIX_VALUES: tuple[str, ...] = ()
_DEFAULT_CATEGORY_VALUES: tuple[str, ...] = (
    "app", "infra", "lib", "ops", "doc", "poc",
)
_DEFAULT_DOMAIN_VALUES: tuple[str, ...] = ()
_DEFAULT_SERVICE_VALUES: tuple[str, ...] = ()
_DEFAULT_TYPE_VALUES: tuple[str, ...] = (
    "agw", "api", "worker", "web", "webview",
    "ios", "android", "win", "did",
    "sdk", "schema",
    "tfstate", "tfmod", "k8s",
    "docs",
)

_DEFAULT_MIN_TOKENS = 5
_DEFAULT_MAX_TOKENS = 6
_DEFAULT_MAX_LENGTH = 50

_FILENAME = ".teammate-naming.toml"

_TOKEN_RE = re.compile(r"^[a-z0-9-]+$")


# ---------- result type ----------


class Verdict(enum.StrEnum):
    """Outcome of a single name validation."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class ValidationResult:
    """Stable, machine-readable validation result for one name."""

    name: str
    verdict: Verdict
    reason: str = ""
    matched: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.OK

    @property
    def failed(self) -> bool:
        return self.verdict is Verdict.FAIL


# ---------- convention dataclass ----------


@dataclass(frozen=True)
class TokenSpec:
    """A single token slot in the pattern."""

    values: tuple[str, ...] = ()
    strict: bool = True


@dataclass(frozen=True)
class NamingConvention:
    """Effective naming convention loaded from TOML.

    The pattern is fixed (``prefix-category-domain-service[-submodule]-type``).
    Vocabularies and quantitative bounds are team-defined.
    """

    prefix: TokenSpec
    category: TokenSpec
    domain: TokenSpec
    service: TokenSpec
    type: TokenSpec
    submodule_recommend: tuple[str, ...]
    submodule_forbid_duplicating: tuple[str, ...]
    min_tokens: int
    max_tokens: int
    max_length: int
    exceptions: tuple[str, ...]
    language: str
    source: str  # absolute path or "<defaults>"


def _default_convention() -> NamingConvention:
    return NamingConvention(
        prefix=TokenSpec(values=_DEFAULT_PREFIX_VALUES, strict=True),
        category=TokenSpec(values=_DEFAULT_CATEGORY_VALUES, strict=True),
        domain=TokenSpec(values=_DEFAULT_DOMAIN_VALUES, strict=True),
        service=TokenSpec(values=_DEFAULT_SERVICE_VALUES, strict=True),
        type=TokenSpec(values=_DEFAULT_TYPE_VALUES, strict=True),
        submodule_recommend=(),
        submodule_forbid_duplicating=("category", "type"),
        min_tokens=_DEFAULT_MIN_TOKENS,
        max_tokens=_DEFAULT_MAX_TOKENS,
        max_length=_DEFAULT_MAX_LENGTH,
        exceptions=(),
        language="en",
        source="<defaults>",
    )


# ---------- TOML loading ----------


def _read_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in value)
    return ()


def _token_spec_from_section(
    section: dict[str, Any] | None,
    default: TokenSpec,
) -> TokenSpec:
    if not isinstance(section, dict):
        return default
    values = _coerce_str_tuple(section.get("values"))
    if not values:
        values = default.values
    strict = bool(section.get("strict", default.strict))
    return TokenSpec(values=values, strict=strict)


def load_naming_convention(path: Path | None) -> NamingConvention | None:
    """Load a ``.teammate-naming.toml`` from ``path``.

    Returns ``None`` if ``path`` is ``None`` or the file is absent. A
    malformed TOML returns ``None`` too — callers either skip the check
    or surface the absence to the user.
    """
    if path is None:
        return None
    data = _read_toml(path)
    if data is None:
        return None

    base = _default_convention()

    tokens = data.get("token") or {}
    prefix = _token_spec_from_section(tokens.get("prefix"), base.prefix)
    category = _token_spec_from_section(tokens.get("category"), base.category)
    domain = _token_spec_from_section(tokens.get("domain"), base.domain)
    service = _token_spec_from_section(tokens.get("service"), base.service)
    type_ = _token_spec_from_section(tokens.get("type"), base.type)

    submodule_section = tokens.get("submodule") or {}
    if not isinstance(submodule_section, dict):
        submodule_section = {}
    sub_recommend = _coerce_str_tuple(submodule_section.get("recommend"))
    sub_forbid = _coerce_str_tuple(
        submodule_section.get("forbid_duplicating")
    ) or base.submodule_forbid_duplicating

    constraints = data.get("constraints") or {}
    if not isinstance(constraints, dict):
        constraints = {}
    try:
        min_tokens = int(constraints.get("min_tokens", base.min_tokens))
    except (TypeError, ValueError):
        min_tokens = base.min_tokens
    try:
        max_tokens = int(constraints.get("max_tokens", base.max_tokens))
    except (TypeError, ValueError):
        max_tokens = base.max_tokens
    try:
        max_length = int(constraints.get("max_length", base.max_length))
    except (TypeError, ValueError):
        max_length = base.max_length

    exceptions_section = data.get("exceptions") or {}
    if not isinstance(exceptions_section, dict):
        exceptions_section = {}
    exceptions = _coerce_str_tuple(exceptions_section.get("allow"))

    locale_section = data.get("locale") or {}
    if not isinstance(locale_section, dict):
        locale_section = {}
    language = str(locale_section.get("language", base.language)).lower()
    if language not in {"en", "ko"}:
        language = "en"

    return NamingConvention(
        prefix=prefix,
        category=category,
        domain=domain,
        service=service,
        type=type_,
        submodule_recommend=sub_recommend,
        submodule_forbid_duplicating=sub_forbid,
        min_tokens=min_tokens,
        max_tokens=max_tokens,
        max_length=max_length,
        exceptions=exceptions,
        language=language,
        source=str(path.resolve()),
    )


def find_naming_config(brain_root: Path) -> Path | None:
    """Search for a ``.teammate-naming.toml`` at the brain root."""
    candidate = brain_root / _FILENAME
    return candidate if candidate.is_file() else None


# ---------- locale messages ----------

# Both tables MUST stay in lock-step. Each key is a stable id.
# The Korean strings are ported from check-repo-name.sh verbatim where
# the rule maps; new keys (prefix, min_tokens distinct from max_tokens,
# explicit length warn) are translated to the same voice.

_MESSAGES_EN: dict[str, str] = {
    "charset": "disallowed character (allowed: [a-z0-9-])",
    "consecutive_hyphens": "contains consecutive hyphens ('--')",
    "edge_hyphen": "leading or trailing hyphen",
    "empty_token": "empty token",
    "leading_digit": "token '{token}' must not start with a digit",
    "uppercase": "token '{token}' must be lowercase",
    "prefix_required": "first token '{token}' is not in allowed prefixes {values}",
    "prefix_empty_dict": "no prefixes configured — define [token.prefix].values",
    "category_required": "category '{token}' is not in allowed values {values}",
    "domain_required": "domain '{token}' is not in allowed values {values}",
    "service_required": (
        "service '{token}' is not in the dictionary {values} — "
        "open a PR to add it"
    ),
    "type_required": "type suffix '{token}' is not in allowed values {values}",
    "token_count": (
        "token count must be {min}..{max} (got {got}; "
        "format: {prefix}-{category}-{domain}-{service}[-{submodule}]-{type})"
    ),
    "duplicate_token": (
        "token '{token}' appears more than once — same token at two "
        "positions is forbidden"
    ),
    "submodule_dup_category": (
        "submodule '{token}' duplicates a category token — pick a "
        "service-meaningful submodule (e.g. 'admin', 'partner')"
    ),
    "submodule_dup_type": (
        "submodule '{token}' duplicates a type token — pick a "
        "service-meaningful submodule (e.g. 'admin', 'partner')"
    ),
    "submodule_unrecommended": (
        "submodule '{token}' is not in the recommended set {values}"
    ),
    "length_warn": "length {length} > {max} (recommended max exceeded)",
}

_MESSAGES_KO: dict[str, str] = {
    "charset": "허용되지 않은 문자 포함 (허용: [a-z0-9-])",
    "consecutive_hyphens": "연속된 하이픈('--') 포함",
    "edge_hyphen": "선두 또는 말미 하이픈",
    "empty_token": "빈 토큰",
    "leading_digit": "토큰 '{token}' 이(가) 숫자로 시작함",
    "uppercase": "토큰 '{token}' 이(가) 소문자로 시작하지 않음",
    "prefix_required": "접두사 '{token}' 은(는) 허용 목록에 없음 {values}",
    "prefix_empty_dict": "접두사 사전이 비어 있음 — [token.prefix].values 를 정의하세요",
    "category_required": "구분 '{token}' 은(는) 허용 목록에 없음 {values}",
    "domain_required": "도메인 '{token}' 은(는) 허용 목록에 없음 {values}",
    "service_required": (
        "서비스 '{token}' 은(는) 사전에 없음 {values} — "
        "사전 추가 PR 을 먼저 제출하세요"
    ),
    "type_required": "타입 접미사 '{token}' 은(는) 허용 목록에 없음 {values}",
    "token_count": (
        "토큰 수는 {min}–{max}개여야 함 (현재 {got}개, "
        "포맷: {prefix}-{category}-{domain}-{service}[-{submodule}]-{type})"
    ),
    "duplicate_token": (
        "토큰 '{token}' 이(가) 여러 위치에 중복됩니다 "
        "(한 이름 안에서 같은 토큰을 다른 자리에 재사용 금지)"
    ),
    "submodule_dup_category": (
        "서브모듈 '{token}' 이(가) 카테고리 토큰과 중복 — "
        "의미 있는 서브모듈을 사용하세요 (예: 'admin', 'partner')"
    ),
    "submodule_dup_type": (
        "서브모듈 '{token}' 이(가) 타입 토큰과 중복 — "
        "의미 있는 서브모듈을 사용하세요 (예: 'admin', 'partner')"
    ),
    "submodule_unrecommended": (
        "서브모듈 '{token}' 이(가) 권장 집합 {values} 에 없음"
    ),
    "length_warn": "길이 {length} > {max} (권장 초과)",
}


def _msg(language: str, key: str, **fmt: Any) -> str:
    table = _MESSAGES_KO if language == "ko" else _MESSAGES_EN
    template = table.get(key) or _MESSAGES_EN[key]
    if "values" in fmt and isinstance(fmt["values"], tuple | list):
        fmt["values"] = "{" + ", ".join(fmt["values"]) + "}"
    return template.format(**fmt)


# ---------- core validator ----------


def _ok(name: str, **matched: Any) -> ValidationResult:
    return ValidationResult(name=name, verdict=Verdict.OK, matched=dict(matched))


def _fail(name: str, reason: str, **matched: Any) -> ValidationResult:
    return ValidationResult(
        name=name, verdict=Verdict.FAIL, reason=reason, matched=dict(matched)
    )


def _warn(name: str, reason: str, **matched: Any) -> ValidationResult:
    return ValidationResult(
        name=name, verdict=Verdict.WARN, reason=reason, matched=dict(matched)
    )


def validate_name(name: str, conv: NamingConvention) -> ValidationResult:
    """Validate ``name`` against ``conv``. Returns a ``ValidationResult``.

    The check sequence mirrors the reference shell validator. The first
    failing rule short-circuits — we don't pile up reasons.
    """
    lang = conv.language

    # Rule 11: exception allowlist.
    if name in conv.exceptions:
        return _ok(name, exception=True)

    # Rule 1: charset (the whole string, including hyphens).
    if not _TOKEN_RE.fullmatch(name):
        return _fail(name, _msg(lang, "charset"))
    if "--" in name:
        return _fail(name, _msg(lang, "consecutive_hyphens"))
    if name.startswith("-") or name.endswith("-"):
        return _fail(name, _msg(lang, "edge_hyphen"))

    tokens = name.split("-")

    # Per-token sanity (charset is enforced by the regex above; we still
    # need to catch leading-digit tokens explicitly because [a-z0-9-]
    # alone allows "1foo").
    for tok in tokens:
        if not tok:
            return _fail(name, _msg(lang, "empty_token"))
        if tok[0].isdigit():
            return _fail(name, _msg(lang, "leading_digit", token=tok))

    n = len(tokens)

    # Rule 2: token count.
    if n < conv.min_tokens or n > conv.max_tokens:
        return _fail(
            name,
            _msg(
                lang,
                "token_count",
                min=conv.min_tokens,
                max=conv.max_tokens,
                got=n,
                prefix="prefix",
                category="category",
                domain="domain",
                service="service",
                submodule="submodule",
                type="type",
            ),
            tokens=list(tokens),
        )

    # Rule 3: prefix.
    if conv.prefix.values:
        if tokens[0] not in conv.prefix.values:
            return _fail(
                name,
                _msg(lang, "prefix_required", token=tokens[0], values=conv.prefix.values),
                prefix=tokens[0],
            )
    elif conv.prefix.strict:
        # Strict mode but no values configured. We don't synthesize a
        # default prefix; we ask the team to declare one.
        return _fail(name, _msg(lang, "prefix_empty_dict"))

    # Slot positions match the pattern:
    #   tokens[0] = prefix
    #   tokens[1] = category
    #   tokens[2] = domain
    #   tokens[3] = service
    #   tokens[-1] = type
    #   tokens[4] (when n == 6) = submodule
    category = tokens[1]
    domain = tokens[2]
    service = tokens[3]
    type_tok = tokens[-1]
    submodule = tokens[4] if n == 6 else None

    # Rule 4: category.
    if conv.category.values and category not in conv.category.values:
        return _fail(
            name,
            _msg(lang, "category_required", token=category, values=conv.category.values),
            category=category,
        )

    # Rule 5: domain.
    if conv.domain.values and domain not in conv.domain.values:
        return _fail(
            name,
            _msg(lang, "domain_required", token=domain, values=conv.domain.values),
            domain=domain,
        )

    # Rule 6: service. Strict mode rejects services not in the dictionary.
    if (
        conv.service.strict
        and conv.service.values
        and service not in conv.service.values
    ):
        return _fail(
            name,
            _msg(
                lang,
                "service_required",
                token=service,
                values=conv.service.values,
            ),
            service=service,
        )

    # Rule 7: type (last token).
    if conv.type.values and type_tok not in conv.type.values:
        return _fail(
            name,
            _msg(lang, "type_required", token=type_tok, values=conv.type.values),
            type=type_tok,
        )

    # Rule 9: no duplicate tokens within a single name. (Kept BEFORE
    # the submodule-specific check so we surface the most general
    # rule first.)
    seen: set[str] = set()
    for tok in tokens:
        if tok in seen:
            return _fail(
                name,
                _msg(lang, "duplicate_token", token=tok),
                duplicate=tok,
            )
        seen.add(tok)

    # Rule 8: submodule constraints (only when present).
    submodule_warn: str | None = None
    if submodule is not None:
        forbid = conv.submodule_forbid_duplicating
        if "category" in forbid and submodule in conv.category.values:
            return _fail(
                name,
                _msg(lang, "submodule_dup_category", token=submodule),
                submodule=submodule,
            )
        if "type" in forbid and submodule in conv.type.values:
            return _fail(
                name,
                _msg(lang, "submodule_dup_type", token=submodule),
                submodule=submodule,
            )
        if conv.submodule_recommend and submodule not in conv.submodule_recommend:
            submodule_warn = _msg(
                lang,
                "submodule_unrecommended",
                token=submodule,
                values=conv.submodule_recommend,
            )

    # Rule 10: length is a soft warn, not a fail. Length warn supersedes
    # submodule warn when both fire — length is the cheaper signal to act on.
    if len(name) > conv.max_length:
        return _warn(
            name,
            _msg(lang, "length_warn", length=len(name), max=conv.max_length),
            length=len(name),
        )

    if submodule_warn is not None:
        return _warn(name, submodule_warn, submodule=submodule)

    return _ok(
        name,
        prefix=tokens[0],
        category=category,
        domain=domain,
        service=service,
        submodule=submodule,
        type=type_tok,
    )


# ---------- starter templates ----------


_TEMPLATE_BODIES: dict[str, str] = {
    "nexus-style": """\
# .teammate-naming.toml — nexus-style starter
# Pattern: {prefix}-{category}-{domain}-{service}[-{submodule}]-{type}
#
# This template mirrors the structural pattern of the canonical multi-account,
# domain-sharded layout. Replace `acme` with your org slug and add real
# services to [token.service].values via the PR-to-add-service workflow.

[locale]
language = "en"

[token.prefix]
values = ["acme"]
strict = true

[token.category]
values = ["app", "infra", "lib", "ops", "doc", "poc"]
strict = true

[token.domain]
# Domain codes map 1:1 to your AWS / cloud account boundaries. Two letters
# is a strong default — short enough not to bloat repo names, distinctive
# enough to grep. `shared` covers org-wide repos that don't belong to one
# domain.
values = ["core", "data", "platform", "shared"]
strict = true

[token.service]
# Service dictionary. Strict mode rejects undeclared services — adding a
# new service is a PR to this table, by design. See docs/NAMING.md.
values = ["billing", "identity", "pricing"]
strict = true

[token.submodule]
# Submodules are exceptional. Only used when one service legitimately
# needs multiple repos of the same type (admin/partner web, b2c/b2b api).
recommend = ["admin", "partner", "consumer", "merchant", "b2b", "b2c"]
forbid_duplicating = ["category", "type"]

[token.type]
values = [
  "agw", "api", "worker", "web", "webview",
  "ios", "android", "win", "did",
  "sdk", "schema",
  "tfstate", "tfmod", "k8s",
  "docs",
]
strict = true

[constraints]
min_tokens = 5
max_tokens = 6
max_length = 50

[exceptions]
# Legacy / brand-mandated names that pre-date this convention.
allow = []
""",
    "small-team": """\
# .teammate-naming.toml — small-team starter
# Pattern: {prefix}-{category}-{domain}-{service}-{type}
#
# Three categories, one shared domain, no submodule support. Good for
# teams under ~10 engineers with a single AWS account or single cluster.

[locale]
language = "en"

[token.prefix]
values = ["acme"]
strict = true

[token.category]
values = ["app", "infra", "ops"]
strict = true

[token.domain]
values = ["shared"]
strict = true

[token.service]
values = ["billing", "identity", "pricing"]
strict = true

[token.submodule]
# Submodule positions exist but the recommended set is empty. With
# max_tokens = 5 below, the schema effectively forbids submodules.
recommend = []
forbid_duplicating = ["category", "type"]

[token.type]
values = ["api", "worker", "web", "sdk", "tfmod", "k8s", "docs"]
strict = true

[constraints]
min_tokens = 5
max_tokens = 5
max_length = 40

[exceptions]
allow = []
""",
    "monorepo-only": """\
# .teammate-naming.toml — monorepo-only starter
# Pattern: {category}-{service}-{type}
#
# A single repo. The convention applies to top-level package / app
# directory names, not repo names. The prefix slot is unused.

[locale]
language = "en"

[token.prefix]
# Empty; the first token is treated as `category` directly. Set strict
# = false so the validator does not demand a prefix dictionary.
values = []
strict = false

[token.category]
values = ["app", "lib", "infra", "ops", "doc"]
strict = true

[token.domain]
# Single domain — every package belongs to the same root.
values = ["mono"]
strict = true

[token.service]
values = ["billing", "identity", "pricing"]
strict = true

[token.submodule]
recommend = []
forbid_duplicating = ["category", "type"]

[token.type]
values = ["api", "worker", "web", "sdk", "tfmod"]
strict = true

[constraints]
# 4 tokens minimum to permit `category-domain-service-type`; 5 allows
# an optional submodule. The shipped pattern is still 5..6 — adopters
# of monorepo-only typically reduce min_tokens via this template.
min_tokens = 4
max_tokens = 5
max_length = 40

[exceptions]
allow = []
""",
    "strict-iac": """\
# .teammate-naming.toml — strict-iac starter
# Pattern: {prefix}-{category}-{domain}-{service}[-{submodule}]-{type}
#
# IaC-only. Categories trimmed to {infra, ops}. Type list trimmed to
# the four shapes that belong in a Terraform / Kubernetes monorepo
# galaxy. Service list deliberately small — IaC services are usually
# the platform shape itself, not product names.

[locale]
language = "en"

[token.prefix]
values = ["acme"]
strict = true

[token.category]
values = ["infra", "ops"]
strict = true

[token.domain]
values = ["core", "data", "platform", "shared"]
strict = true

[token.service]
values = ["network", "compute", "observability", "secrets", "ci"]
strict = true

[token.submodule]
recommend = ["dev", "stage", "prod"]
forbid_duplicating = ["category", "type"]

[token.type]
values = ["tfmod", "tfstate", "k8s", "docs"]
strict = true

[constraints]
min_tokens = 5
max_tokens = 6
max_length = 50

[exceptions]
allow = []
""",
}


def list_templates() -> tuple[str, ...]:
    return tuple(_TEMPLATE_BODIES)


def render_template(template: str) -> str:
    """Return the TOML body for ``template``. Raises ``KeyError`` if absent."""
    if template not in _TEMPLATE_BODIES:
        raise KeyError(template)
    return _TEMPLATE_BODIES[template]


def write_starter(target: Path, template: str, *, force: bool = False) -> Path:
    """Write a starter ``.teammate-naming.toml`` to ``target``.

    Refuses to overwrite an existing file unless ``force`` is true.
    Returns the resolved path written.
    """
    body = render_template(template)
    if target.exists() and not force:
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target.resolve()


__all__ = [
    "NamingConvention",
    "TokenSpec",
    "ValidationResult",
    "Verdict",
    "find_naming_config",
    "list_templates",
    "load_naming_convention",
    "render_template",
    "validate_name",
    "write_starter",
]
