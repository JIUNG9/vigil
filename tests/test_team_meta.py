"""Tests for the v0.10 ``_team_meta`` parser shared by radar / digest / drafter."""

from __future__ import annotations

from pathlib import Path

from vigil.agent._team_meta import (
    Engineer,
    load_team_meta,
    parse_people,
    parse_services,
)


def test_parse_people_bullet_form(tmp_path: Path):
    p = tmp_path / "people.md"
    p.write_text(
        "# People\n\n"
        "- alice <alice@team> — Auth Service owner\n"
        "- bob <bob@team>\n"
        "- carol <carol@example.com> — Network team\n",
        encoding="utf-8",
    )
    engineers = parse_people(p)
    assert len(engineers) == 3
    assert engineers[0] == Engineer(id="alice", email="alice@team",
                                     role="Auth Service owner")
    assert engineers[1].id == "bob"
    assert engineers[2].email == "carol@example.com"


def test_parse_people_table_form(tmp_path: Path):
    p = tmp_path / "people.md"
    p.write_text(
        "# Team\n\n"
        "| id    | email           | role             |\n"
        "|-------|-----------------|------------------|\n"
        "| alice | alice@team      | Auth owner       |\n"
        "| bob   | bob@example.com | Platform         |\n",
        encoding="utf-8",
    )
    engineers = parse_people(p)
    assert len(engineers) == 2
    assert engineers[0].id == "alice"
    assert engineers[0].email == "alice@team"
    assert engineers[1].email == "bob@example.com"


def test_parse_people_empty_or_missing(tmp_path: Path):
    assert parse_people(tmp_path / "missing.md") == []
    p = tmp_path / "people.md"
    p.write_text("# nothing here\n", encoding="utf-8")
    assert parse_people(p) == []


def test_parse_services_bullet_form(tmp_path: Path):
    p = tmp_path / "services.md"
    p.write_text(
        "# Services\n\n"
        "- auth-service: alice — owns aws_iam_role.deploy-bot, vpc-abc12345\n"
        "- billing: bob — owns aws_db_instance.billing-primary\n",
        encoding="utf-8",
    )
    services = parse_services(p)
    assert len(services) == 2
    auth = services[0]
    assert auth.name == "auth-service"
    assert auth.owner == "alice"
    assert "aws_iam_role.deploy-bot" in auth.resources
    assert "vpc-abc12345" in auth.resources


def test_parse_services_resource_match(tmp_path: Path):
    p = tmp_path / "services.md"
    p.write_text(
        "- auth-service: alice — owns aws_iam_role.deploy-bot\n",
        encoding="utf-8",
    )
    services = parse_services(p)
    svc = services[0]
    assert svc.matches_resource("aws_iam_role.deploy-bot")
    assert svc.matches_resource("deploy-bot")
    assert not svc.matches_resource("aws_vpc.shared")


def test_parse_services_table_form(tmp_path: Path):
    p = tmp_path / "services.md"
    p.write_text(
        "| service       | owner | resources                   |\n"
        "|---------------|-------|-----------------------------|\n"
        "| auth-service  | alice | aws_iam_role.deploy-bot     |\n"
        "| billing       | bob   | aws_db_instance.billing-primary |\n",
        encoding="utf-8",
    )
    services = parse_services(p)
    names = {s.name for s in services}
    assert names == {"auth-service", "billing"}


def test_load_team_meta_combined(tmp_path: Path):
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "people.md").write_text(
        "- alice <alice@team> — Auth\n", encoding="utf-8",
    )
    (tmp_path / "knowledge" / "services.md").write_text(
        "- auth-service: alice — owns aws_iam_role.deploy-bot\n",
        encoding="utf-8",
    )
    meta = load_team_meta(tmp_path)
    assert meta.engineers
    assert meta.services
    assert meta.engineer_by_email("alice@team").id == "alice"
    assert meta.engineer_by_id("alice") == meta.engineers[0]
    assert meta.engineer_by_id("nobody") is None
    matches = meta.services_owning_resource("aws_iam_role.deploy-bot")
    assert matches and matches[0].name == "auth-service"
