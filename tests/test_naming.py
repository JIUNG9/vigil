"""Tests for the configurable naming convention (v0.7).

Mirrors the reference shell validator at ``check-repo-name.sh``:
  - same rule semantics (charset, count, dictionary lookups, duplicate
    tokens, length warn).
  - same exit codes for the CLI (0 / 1 / 2).

Plus the v0.7 additions: configurable vocabularies, Korean locale,
templates, and validate integration.
"""

from __future__ import annotations

import textwrap
import tomllib
from pathlib import Path

from click.testing import CliRunner

from vigil.cli import main as cli_main
from vigil.naming import (
    NamingConvention,
    TokenSpec,
    Verdict,
    list_templates,
    load_naming_convention,
    render_template,
    validate_name,
    write_starter,
)
from vigil.validate import validate as run_validate

# ---------- helpers ----------


def _nexus_like_convention(language: str = "en") -> NamingConvention:
    """An in-memory convention that mirrors the canonical pattern with
    generic vocabulary. Use this for the bulk of pure-validator tests
    so we don't have to touch the filesystem."""
    return NamingConvention(
        prefix=TokenSpec(values=("acme",), strict=True),
        category=TokenSpec(
            values=("app", "infra", "lib", "ops", "doc", "poc"),
            strict=True,
        ),
        domain=TokenSpec(
            values=("core", "data", "platform", "shared"),
            strict=True,
        ),
        service=TokenSpec(
            values=("billing", "identity", "pricing"),
            strict=True,
        ),
        type=TokenSpec(
            values=(
                "agw", "api", "worker", "web", "webview",
                "ios", "android", "win", "did",
                "sdk", "schema",
                "tfstate", "tfmod", "k8s",
                "docs",
            ),
            strict=True,
        ),
        submodule_recommend=(
            "admin", "partner", "consumer", "merchant", "b2b", "b2c",
        ),
        submodule_forbid_duplicating=("category", "type"),
        min_tokens=5,
        max_tokens=6,
        max_length=50,
        exceptions=("acme-docs",),
        language=language,
        source="<test>",
    )


def _write_naming_toml(root: Path, body: str) -> Path:
    target = root / ".teammate-naming.toml"
    target.write_text(textwrap.dedent(body), encoding="utf-8")
    return target


def _seed_brain(root: Path) -> None:
    (root / "CLAUDE.md").write_text("# brain\n", encoding="utf-8")


# ---------- charset / format ----------


def test_charset_rejects_uppercase():
    conv = _nexus_like_convention()
    res = validate_name("acme-app-core-Billing-api", conv)
    assert res.verdict is Verdict.FAIL
    assert "[a-z0-9-]" in res.reason


def test_charset_rejects_underscore():
    conv = _nexus_like_convention()
    res = validate_name("acme_app_core_billing_api", conv)
    assert res.verdict is Verdict.FAIL


def test_charset_rejects_space():
    conv = _nexus_like_convention()
    res = validate_name("acme app core billing api", conv)
    assert res.verdict is Verdict.FAIL


def test_charset_rejects_consecutive_hyphens():
    conv = _nexus_like_convention()
    res = validate_name("acme--app-core-billing-api", conv)
    assert res.verdict is Verdict.FAIL
    assert "--" in res.reason or "consecutive" in res.reason.lower()


def test_charset_rejects_leading_hyphen():
    conv = _nexus_like_convention()
    res = validate_name("-acme-app-core-billing-api", conv)
    assert res.verdict is Verdict.FAIL


def test_charset_rejects_trailing_hyphen():
    conv = _nexus_like_convention()
    res = validate_name("acme-app-core-billing-api-", conv)
    assert res.verdict is Verdict.FAIL


def test_charset_rejects_token_starting_with_digit():
    conv = _nexus_like_convention()
    res = validate_name("acme-app-core-billing-2api", conv)
    assert res.verdict is Verdict.FAIL
    # "leading digit" is the canonical reason here.
    assert "digit" in res.reason.lower() or "1" in res.reason or "2" in res.reason


# ---------- token count ----------


def test_token_count_too_few():
    conv = _nexus_like_convention()
    res = validate_name("acme-app-billing-api", conv)
    assert res.verdict is Verdict.FAIL


def test_token_count_too_many():
    conv = _nexus_like_convention()
    # 7 tokens, but distinct so we hit the count check before dup check.
    res = validate_name("acme-app-core-billing-admin-extra-api", conv)
    assert res.verdict is Verdict.FAIL


def test_token_count_exactly_five_ok():
    conv = _nexus_like_convention()
    res = validate_name("acme-infra-core-billing-tfmod", conv)
    assert res.verdict is Verdict.OK


def test_token_count_exactly_six_ok_with_submodule():
    conv = _nexus_like_convention()
    res = validate_name("acme-app-shared-billing-admin-web", conv)
    assert res.verdict is Verdict.OK
    assert res.matched["submodule"] == "admin"


# ---------- dictionary lookups ----------


def test_prefix_must_match_dictionary():
    conv = _nexus_like_convention()
    res = validate_name("xyz-app-core-billing-api", conv)
    assert res.verdict is Verdict.FAIL
    assert "xyz" in res.reason


def test_category_must_match_dictionary():
    conv = _nexus_like_convention()
    res = validate_name("acme-svc-core-billing-api", conv)
    assert res.verdict is Verdict.FAIL
    assert "svc" in res.reason


def test_domain_must_match_dictionary():
    conv = _nexus_like_convention()
    res = validate_name("acme-app-galaxy-billing-api", conv)
    assert res.verdict is Verdict.FAIL
    assert "galaxy" in res.reason


def test_service_strict_rejects_unknown():
    conv = _nexus_like_convention()
    res = validate_name("acme-app-core-thinger-api", conv)
    assert res.verdict is Verdict.FAIL
    assert "thinger" in res.reason


def test_service_non_strict_allows_unknown():
    base = _nexus_like_convention()
    relaxed = NamingConvention(
        prefix=base.prefix,
        category=base.category,
        domain=base.domain,
        service=TokenSpec(values=base.service.values, strict=False),
        type=base.type,
        submodule_recommend=base.submodule_recommend,
        submodule_forbid_duplicating=base.submodule_forbid_duplicating,
        min_tokens=base.min_tokens,
        max_tokens=base.max_tokens,
        max_length=base.max_length,
        exceptions=base.exceptions,
        language=base.language,
        source=base.source,
    )
    res = validate_name("acme-app-core-thinger-api", relaxed)
    assert res.verdict is Verdict.OK


def test_type_must_match_dictionary():
    conv = _nexus_like_convention()
    res = validate_name("acme-app-core-billing-thingy", conv)
    assert res.verdict is Verdict.FAIL
    assert "thingy" in res.reason


def test_type_must_be_last_token():
    """Type-token-last is structural — a name where the type appears
    earlier still fails because the LAST token must be the type, and a
    type word in a non-last slot lands in a slot that rejects it."""
    conv = _nexus_like_convention()
    # Type token "api" in domain slot — domain rejects it.
    res = validate_name("acme-app-api-billing-web", conv)
    assert res.verdict is Verdict.FAIL


# ---------- duplicate-token rule ----------


def test_duplicate_token_rejected_category_submodule():
    """The marquee duplicate case from check-repo-name.sh: 'app' at
    category and submodule positions."""
    conv = _nexus_like_convention()
    res = validate_name("acme-app-shared-billing-app-api", conv)
    assert res.verdict is Verdict.FAIL


def test_duplicate_token_rejected_anywhere():
    """Same token at any two positions trips rule 9, even when both
    positions individually accept the token."""
    base = _nexus_like_convention()
    # `core` is a valid domain. We deliberately add it to the service
    # dictionary so the service-slot lookup passes — leaving the
    # duplicate-tokens rule as the only thing standing in the way.
    conv = NamingConvention(
        prefix=base.prefix,
        category=base.category,
        domain=base.domain,
        service=TokenSpec(values=("billing", "identity", "core"), strict=True),
        type=base.type,
        submodule_recommend=base.submodule_recommend,
        submodule_forbid_duplicating=base.submodule_forbid_duplicating,
        min_tokens=base.min_tokens,
        max_tokens=base.max_tokens,
        max_length=base.max_length,
        exceptions=base.exceptions,
        language=base.language,
        source=base.source,
    )
    res = validate_name("acme-app-core-core-api", conv)
    assert res.verdict is Verdict.FAIL


# ---------- exceptions ----------


def test_exception_passes_unchecked():
    conv = _nexus_like_convention()
    res = validate_name("acme-docs", conv)
    assert res.verdict is Verdict.OK
    assert res.matched.get("exception") is True


def test_exception_with_garbage_still_passes():
    """Exception list is unconditional — the validator does not even
    look at the structural rules for an exempted name."""
    base = _nexus_like_convention()
    conv = NamingConvention(
        prefix=base.prefix,
        category=base.category,
        domain=base.domain,
        service=base.service,
        type=base.type,
        submodule_recommend=base.submodule_recommend,
        submodule_forbid_duplicating=base.submodule_forbid_duplicating,
        min_tokens=base.min_tokens,
        max_tokens=base.max_tokens,
        max_length=base.max_length,
        exceptions=("not-a-real-name",),
        language=base.language,
        source=base.source,
    )
    res = validate_name("not-a-real-name", conv)
    assert res.verdict is Verdict.OK


# ---------- length warn (NOT fail) ----------


def test_length_over_max_emits_warn_not_fail():
    base = _nexus_like_convention()
    # Tighten max_length so we trip it without absurd names.
    conv = NamingConvention(
        prefix=base.prefix,
        category=base.category,
        domain=base.domain,
        service=base.service,
        type=base.type,
        submodule_recommend=base.submodule_recommend,
        submodule_forbid_duplicating=base.submodule_forbid_duplicating,
        min_tokens=base.min_tokens,
        max_tokens=base.max_tokens,
        max_length=20,
        exceptions=base.exceptions,
        language=base.language,
        source=base.source,
    )
    res = validate_name("acme-infra-core-billing-tfmod", conv)
    assert res.verdict is Verdict.WARN
    assert "length" in res.reason.lower() or "길이" in res.reason


# ---------- submodule rules ----------


def test_submodule_absent_ok():
    conv = _nexus_like_convention()
    res = validate_name("acme-infra-core-billing-tfmod", conv)
    assert res.verdict is Verdict.OK
    assert res.matched["submodule"] is None


def test_submodule_recommended_ok():
    conv = _nexus_like_convention()
    res = validate_name("acme-app-shared-billing-partner-web", conv)
    assert res.verdict is Verdict.OK


def test_submodule_unrecommended_warns():
    conv = _nexus_like_convention()
    # `legacy` is not in the recommended set; structurally legal but warned.
    res = validate_name("acme-app-shared-billing-legacy-web", conv)
    assert res.verdict is Verdict.WARN


def test_submodule_duplicating_category_fails():
    conv = _nexus_like_convention()
    res = validate_name("acme-app-shared-billing-infra-web", conv)
    assert res.verdict is Verdict.FAIL


def test_submodule_duplicating_type_fails():
    conv = _nexus_like_convention()
    # `api` is a type token; using it as the submodule duplicates type.
    # We pick `web` as the actual type slot so the name parses through
    # the slot-by-slot check.
    res = validate_name("acme-app-shared-billing-api-web", conv)
    assert res.verdict is Verdict.FAIL


# ---------- locale ----------


def test_locale_en_message_text():
    conv = _nexus_like_convention(language="en")
    res = validate_name("acme-svc-core-billing-api", conv)
    assert res.verdict is Verdict.FAIL
    assert "category" in res.reason.lower() or "svc" in res.reason


def test_locale_ko_message_text():
    conv = _nexus_like_convention(language="ko")
    res = validate_name("acme-svc-core-billing-api", conv)
    assert res.verdict is Verdict.FAIL
    # Korean reason text — characters from check-repo-name.sh.
    assert "구분" in res.reason or "허용" in res.reason


def test_locale_en_and_ko_differ_for_same_input():
    conv_en = _nexus_like_convention(language="en")
    conv_ko = _nexus_like_convention(language="ko")
    name = "acme-svc-core-billing-api"
    res_en = validate_name(name, conv_en)
    res_ko = validate_name(name, conv_ko)
    assert res_en.verdict is Verdict.FAIL
    assert res_ko.verdict is Verdict.FAIL
    assert res_en.reason != res_ko.reason


# ---------- TOML loader ----------


def test_load_naming_convention_from_toml(tmp_path: Path):
    cfg = _write_naming_toml(
        tmp_path,
        """
        [locale]
        language = "ko"

        [token.prefix]
        values = ["acme"]
        strict = true

        [token.category]
        values = ["app", "infra"]

        [token.domain]
        values = ["core", "shared"]

        [token.service]
        values = ["billing"]
        strict = true

        [token.type]
        values = ["api", "tfmod"]

        [constraints]
        min_tokens = 5
        max_tokens = 5
        max_length = 30

        [exceptions]
        allow = ["acme-legacy"]
        """,
    )
    conv = load_naming_convention(cfg)
    assert conv is not None
    assert conv.language == "ko"
    assert conv.prefix.values == ("acme",)
    assert conv.exceptions == ("acme-legacy",)
    assert conv.max_length == 30
    # Round-trip through validate_name.
    assert validate_name("acme-infra-core-billing-tfmod", conv).verdict is Verdict.OK
    assert validate_name("acme-legacy", conv).verdict is Verdict.OK


def test_load_naming_convention_missing_returns_none(tmp_path: Path):
    cfg = tmp_path / ".teammate-naming.toml"
    assert load_naming_convention(cfg) is None
    assert load_naming_convention(None) is None


# ---------- CLI: naming check ----------


def test_cli_naming_check_ok(tmp_path: Path, monkeypatch):
    _write_naming_toml(
        tmp_path,
        """
        [token.prefix]
        values = ["acme"]
        [token.category]
        values = ["infra"]
        [token.domain]
        values = ["core"]
        [token.service]
        values = ["billing"]
        [token.type]
        values = ["tfmod"]
        [constraints]
        min_tokens = 5
        max_tokens = 6
        max_length = 50
        """,
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["naming", "check", "acme-infra-core-billing-tfmod"])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_cli_naming_check_proprietary_prefix_fails(tmp_path: Path, monkeypatch):
    """Smoke per spec: a name using tokens the team did NOT declare
    fails at the prefix dictionary check, NOT at any hard-coded blocklist.
    The validator only knows what the TOML declares."""
    _write_naming_toml(
        tmp_path,
        """
        [token.prefix]
        values = ["acme"]
        [token.category]
        values = ["infra"]
        [token.domain]
        values = ["core"]
        [token.service]
        values = ["billing"]
        [token.type]
        values = ["tfmod"]
        """,
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    # A name with a different prefix and unrelated tokens — must FAIL
    # at the prefix step. (No blocklist; no built-in rejection.)
    result = runner.invoke(
        cli_main, ["naming", "check", "xyz-infra-zz-foo-tfmod"]
    )
    assert result.exit_code == 1
    # The failure surfaces in stderr per the bash-validator parity.
    assert "FAIL" in (result.stderr or "") + (result.output or "")


def test_cli_naming_check_stdin_batch(tmp_path: Path, monkeypatch):
    _write_naming_toml(
        tmp_path,
        """
        [token.prefix]
        values = ["acme"]
        [token.category]
        values = ["infra", "app"]
        [token.domain]
        values = ["core"]
        [token.service]
        values = ["billing"]
        [token.type]
        values = ["tfmod", "api"]
        """,
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    # First name is OK, second has uppercase. Mixed exit code → 1.
    stdin_payload = "acme-infra-core-billing-tfmod\nacme-App-core-billing-api\n"
    result = runner.invoke(cli_main, ["naming", "check", "-"], input=stdin_payload)
    assert result.exit_code == 1


def test_cli_naming_check_no_config(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["naming", "check", "anything"])
    assert result.exit_code == 2


# ---------- CLI: naming list ----------


def test_cli_naming_list_renders(tmp_path: Path, monkeypatch):
    _write_naming_toml(
        tmp_path,
        """
        [token.prefix]
        values = ["acme"]
        [token.category]
        values = ["infra", "app"]
        [token.domain]
        values = ["core"]
        [token.service]
        values = ["billing"]
        [token.type]
        values = ["tfmod"]
        """,
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["naming", "list"])
    assert result.exit_code == 0
    assert "acme" in result.output
    assert "tfmod" in result.output


# ---------- CLI: naming init ----------


def test_cli_naming_init_template_writes_round_trippable_toml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["naming", "init", "--template", "nexus-style"])
    assert result.exit_code == 0
    target = tmp_path / ".teammate-naming.toml"
    assert target.is_file()
    # Parses as TOML.
    with target.open("rb") as fh:
        tomllib.load(fh)
    # Loader reads it back into a NamingConvention with non-empty vocabularies.
    conv = load_naming_convention(target)
    assert conv is not None
    assert conv.prefix.values == ("acme",)
    assert "tfmod" in conv.type.values


def test_cli_naming_init_refuses_overwrite(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    target = tmp_path / ".teammate-naming.toml"
    target.write_text("# existing\n", encoding="utf-8")
    result = runner.invoke(cli_main, ["naming", "init", "--template", "small-team"])
    assert result.exit_code == 1
    # Untouched.
    assert target.read_text() == "# existing\n"


def test_cli_naming_init_force_overwrites(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    target = tmp_path / ".teammate-naming.toml"
    target.write_text("# existing\n", encoding="utf-8")
    result = runner.invoke(
        cli_main, ["naming", "init", "--template", "small-team", "--force"]
    )
    assert result.exit_code == 0
    body = target.read_text()
    assert "small-team" in body or "[token.category]" in body


def test_all_templates_render_and_load(tmp_path: Path):
    """Each shipped template must produce a TOML the loader can parse."""
    for tmpl in list_templates():
        body = render_template(tmpl)
        path = tmp_path / f"{tmpl}.toml"
        path.write_text(body, encoding="utf-8")
        conv = load_naming_convention(path)
        assert conv is not None, f"template {tmpl!r} did not load"
        # Each template either has a real prefix or explicitly opts out.
        if conv.prefix.strict:
            assert conv.prefix.values, f"{tmpl}: strict prefix with empty values"


def test_write_starter_helper_round_trip(tmp_path: Path):
    target = tmp_path / ".teammate-naming.toml"
    written = write_starter(target, "strict-iac")
    assert written == target.resolve()
    conv = load_naming_convention(target)
    assert conv is not None
    assert "tfmod" in conv.type.values


# ---------- validate integration ----------


def test_validate_includes_naming_when_flag_set(tmp_path: Path):
    _seed_brain(tmp_path)
    _write_naming_toml(
        tmp_path,
        """
        [token.prefix]
        values = ["acme"]
        [token.category]
        values = ["docs", "lib"]
        [token.domain]
        values = ["shared"]
        [token.service]
        values = ["handbook"]
        [token.type]
        values = ["docs", "sdk"]
        [constraints]
        min_tokens = 5
        max_tokens = 5
        """,
    )
    # Create one well-named, one ill-named directory under docs/.
    (tmp_path / "docs" / "acme-docs-shared-handbook-docs").mkdir(parents=True)
    (tmp_path / "docs" / "BAD_NAME").mkdir(parents=True)

    report = run_validate(tmp_path, include_naming=True)
    by_name = {c.name: c for c in report.checks}
    assert "naming_convention" in by_name
    assert by_name["naming_convention"].status == "FAIL"


def test_validate_skips_naming_silently_when_no_toml(tmp_path: Path):
    _seed_brain(tmp_path)
    (tmp_path / "docs" / "weird name").mkdir(parents=True)
    report = run_validate(tmp_path, include_naming=True)
    by_name = {c.name: c for c in report.checks}
    # The check is present but PASSes-with-skip.
    assert by_name["naming_convention"].status == "PASS"
    assert "skipped" in by_name["naming_convention"].summary


def test_validate_off_by_default(tmp_path: Path):
    _seed_brain(tmp_path)
    _write_naming_toml(
        tmp_path,
        """
        [token.prefix]
        values = ["acme"]
        [token.category]
        values = ["docs"]
        [token.domain]
        values = ["shared"]
        [token.service]
        values = ["handbook"]
        [token.type]
        values = ["docs"]
        """,
    )
    report = run_validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert "naming_convention" not in by_name


def test_validate_reads_include_naming_from_config_toml(tmp_path: Path):
    _seed_brain(tmp_path)
    _write_naming_toml(
        tmp_path,
        """
        [token.prefix]
        values = ["acme"]
        [token.category]
        values = ["docs"]
        [token.domain]
        values = ["shared"]
        [token.service]
        values = ["handbook"]
        [token.type]
        values = ["docs"]
        """,
    )
    teammate_dir = tmp_path / ".teammate"
    teammate_dir.mkdir()
    (teammate_dir / "config.toml").write_text(
        "[validate]\ninclude_naming = true\n",
        encoding="utf-8",
    )
    report = run_validate(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert "naming_convention" in by_name


# ---------- absent-vocabulary edge cases ----------


def test_strict_prefix_with_empty_values_fails():
    """Strict mode + empty dictionary = always-FAIL. The validator does
    not synthesize a prefix; it asks the team to declare one."""
    base = _nexus_like_convention()
    conv = NamingConvention(
        prefix=TokenSpec(values=(), strict=True),
        category=base.category,
        domain=base.domain,
        service=base.service,
        type=base.type,
        submodule_recommend=base.submodule_recommend,
        submodule_forbid_duplicating=base.submodule_forbid_duplicating,
        min_tokens=base.min_tokens,
        max_tokens=base.max_tokens,
        max_length=base.max_length,
        exceptions=base.exceptions,
        language=base.language,
        source=base.source,
    )
    res = validate_name("acme-infra-core-billing-tfmod", conv)
    assert res.verdict is Verdict.FAIL


def test_non_strict_prefix_with_empty_values_passes_through():
    """Non-strict + empty dictionary = no prefix check at all. This is
    the monorepo-only template's posture."""
    base = _nexus_like_convention()
    conv = NamingConvention(
        prefix=TokenSpec(values=(), strict=False),
        category=base.category,
        domain=base.domain,
        service=base.service,
        type=base.type,
        submodule_recommend=base.submodule_recommend,
        submodule_forbid_duplicating=base.submodule_forbid_duplicating,
        min_tokens=base.min_tokens,
        max_tokens=base.max_tokens,
        max_length=base.max_length,
        exceptions=base.exceptions,
        language=base.language,
        source=base.source,
    )
    # Any prefix-shaped first token passes the prefix step now; the
    # rest of the pipeline still applies.
    res = validate_name("anything-infra-core-billing-tfmod", conv)
    assert res.verdict is Verdict.OK
