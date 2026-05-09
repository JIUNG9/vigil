"""Tests for the CloudTrail → brain-invalidations Lambda handler.

Lives outside the main test suite (``pyproject.toml`` ``testpaths`` is
``tests/``). Run from this directory::

    python -m pytest -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

# Load the handler module by path so tests run from any cwd.
_HANDLER_PATH = Path(__file__).resolve().parent.parent / "handler.py"
_spec = importlib.util.spec_from_file_location("handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
sys.modules["handler"] = handler
_spec.loader.exec_module(handler)


# ---------- helpers ----------


def _eventbridge_event(
    event_name: str = "DetachVpcCidrBlock",
    *,
    region: str = "us-east-1",
    actor_arn: str = "arn:aws:iam::000000000000:user/alice",
    extra: dict | None = None,
) -> dict:
    detail = {
        "eventName": event_name,
        "awsRegion": region,
        "eventSource": "ec2.amazonaws.com",
        "eventTime": "2026-05-09T14:00:00Z",
        "recipientAccountId": "000000000000",
        "userIdentity": {"arn": actor_arn, "type": "IAMUser"},
        "requestParameters": {"vpcId": "vpc-abc12345"},
    }
    if extra:
        detail.update(extra)
    return {
        "version": "0",
        "id": "test-id",
        "detail-type": "AWS API Call via CloudTrail",
        "source": "aws.ec2",
        "detail": detail,
    }


def _set_env(monkeypatch, **kw) -> None:
    monkeypatch.setenv("GITHUB_REPO", kw.get("repo", "your-org/brain-invalidations"))
    monkeypatch.setenv("GITHUB_BRANCH", kw.get("branch", "main"))
    monkeypatch.setenv("GITHUB_PAT_SSM_PARAMETER",
                       kw.get("ssm", "/teammate/github_pat"))
    if "slack" in kw:
        monkeypatch.setenv("SLACK_WEBHOOK_URL", kw["slack"])
    monkeypatch.setenv("SEVERITY_MAP_JSON", kw.get(
        "severity_map",
        json.dumps({
            "DetachVpcCidrBlock": "high",
            "DeleteRole": "critical",
            "ModifyDBInstance": "medium",
        }),
    ))


# ---------- map_event ----------


def test_map_event_extracts_vpc_id(monkeypatch):
    _set_env(monkeypatch)
    out = handler.map_event(_eventbridge_event("DetachVpcCidrBlock"))
    assert out["resource_id"] == "vpc-abc12345"
    assert out["severity"] == "high"
    assert out["action"] == "detach"
    assert out["source"] == "cloudtrail"


def test_map_event_extracts_role_name(monkeypatch):
    _set_env(monkeypatch)
    ev = _eventbridge_event(
        "DeleteRole",
        extra={
            "eventSource": "iam.amazonaws.com",
            "requestParameters": {"roleName": "deploy-bot"},
        },
    )
    out = handler.map_event(ev)
    assert out["resource_id"] == "deploy-bot"
    assert out["severity"] == "critical"
    assert out["action"] == "delete"


def test_map_event_uses_default_severity_for_unknown(monkeypatch):
    _set_env(monkeypatch, severity_map="{}")
    monkeypatch.setenv("DEFAULT_SEVERITY", "medium")
    out = handler.map_event(_eventbridge_event("CreateBucket"))
    assert out["severity"] == "medium"


def test_map_event_unknown_action_falls_back_to_modify(monkeypatch):
    _set_env(monkeypatch)
    out = handler.map_event(_eventbridge_event("WeirdEventName"))
    assert out["action"] == "modify"


def test_map_event_records_actor(monkeypatch):
    _set_env(monkeypatch)
    out = handler.map_event(_eventbridge_event(
        "DetachVpcCidrBlock",
        actor_arn="arn:aws:iam::000000000000:user/bob",
    ))
    assert out["actor"].endswith("user/bob")


def test_map_event_handles_missing_request_parameters(monkeypatch):
    _set_env(monkeypatch)
    bad = _eventbridge_event("DetachVpcCidrBlock",
                             extra={"requestParameters": None})
    out = handler.map_event(bad)
    # No id available → "unknown" sentinel.
    assert out["resource_id"] == "unknown"


def test_map_event_includes_metadata_event_name(monkeypatch):
    _set_env(monkeypatch)
    out = handler.map_event(_eventbridge_event("DetachVpcCidrBlock"))
    assert out["metadata"]["event_name"] == "DetachVpcCidrBlock"
    assert out["metadata"]["aws_region"] == "us-east-1"


# ---------- _slugify (smoke) ----------


def test_slugify_handles_special_chars():
    assert handler._slugify("aws_vpc.shared") == "aws-vpc-shared"
    assert handler._slugify("") == "resource"
    assert handler._slugify("Multiple    Spaces") == "multiple-spaces"


def test_severity_map_falls_back_for_invalid_json(monkeypatch):
    monkeypatch.setenv("SEVERITY_MAP_JSON", "not json")
    monkeypatch.setenv("DEFAULT_SEVERITY", "low")
    assert handler._resolve_severity("Anything") == "low"


def test_severity_map_caps_unknown_levels(monkeypatch):
    monkeypatch.setenv("SEVERITY_MAP_JSON",
                       json.dumps({"DetachVpcCidrBlock": "severe"}))
    monkeypatch.setenv("DEFAULT_SEVERITY", "high")
    # "severe" is not in {low, medium, high, critical}; we fall back to default.
    assert handler._resolve_severity("DetachVpcCidrBlock") == "high"


# ---------- handler entry point ----------


def test_lambda_handler_commits_to_github(monkeypatch):
    _set_env(monkeypatch)

    def _fake_pat(_param):
        return "ghp_dummy_token"

    fake_response = {
        "content": {"path": "invalidations/2026/05/09/x.json"},
        "commit": {"sha": "abc123def456"},
    }

    captured = {}

    def _fake_commit(event_dict, repo, branch, pat):
        captured["event"] = event_dict
        captured["repo"] = repo
        captured["branch"] = branch
        captured["pat"] = pat
        return fake_response

    monkeypatch.setattr(handler, "_get_pat", _fake_pat)
    monkeypatch.setattr(handler, "_commit_to_github", _fake_commit)

    result = handler.lambda_handler(_eventbridge_event("DetachVpcCidrBlock"))
    assert result["status"] == "ok"
    assert result["commit_sha"] == "abc123def456"
    assert captured["repo"] == "your-org/brain-invalidations"
    assert captured["branch"] == "main"
    assert captured["pat"] == "ghp_dummy_token"
    assert captured["event"]["resource_id"] == "vpc-abc12345"


def test_lambda_handler_posts_to_slack_when_configured(monkeypatch):
    _set_env(monkeypatch, slack="https://hooks.slack.example/T/B/X")

    monkeypatch.setattr(handler, "_get_pat", lambda _p: "ghp")
    monkeypatch.setattr(
        handler, "_commit_to_github",
        lambda *_a, **_kw: {"commit": {"sha": "deadbeef"}, "content": {"path": "x"}},
    )
    posted = {}

    def _fake_slack(url, ev):
        posted["url"] = url
        posted["event"] = ev

    monkeypatch.setattr(handler, "_post_slack", _fake_slack)
    handler.lambda_handler(_eventbridge_event())
    assert posted["url"].startswith("https://hooks.slack.example")
    assert posted["event"]["resource_id"] == "vpc-abc12345"


def test_commit_to_github_constructs_valid_url(monkeypatch):
    """Verify the URL + payload that hits the GitHub Contents API."""
    captured = {}

    class _FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["headers"] = dict(req.header_items())
        captured["data"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(json.dumps({
            "commit": {"sha": "00ff"},
            "content": {"path": "ok"},
        }).encode("utf-8"))

    monkeypatch.setattr(handler.urllib.request, "urlopen", _fake_urlopen)
    event_dict = {
        "id": "x" * 32,
        "timestamp": "2026-05-09T14:00:00+00:00",
        "source": "cloudtrail",
        "resource_type": "aws_vpc",
        "resource_id": "vpc-abc12345",
        "action": "detach",
        "severity": "high",
        "actor": "alice",
        "metadata": {"event_name": "DetachVpcCidrBlock"},
    }
    out = handler._commit_to_github(
        event_dict, "your-org/brain-invalidations", "main", "ghp_dummy"
    )
    assert out["commit"]["sha"] == "00ff"
    assert "your-org/brain-invalidations" in captured["url"]
    assert captured["method"] == "PUT"
    # Path must be invalidations/YYYY/MM/DD/...
    assert "invalidations/2026/05/09" in captured["url"]
    # Authorization header carries the bearer token.
    auth = {k.lower(): v for k, v in captured["headers"].items()}["authorization"]
    assert auth == "Bearer ghp_dummy"
    # Body content is a base64-encoded JSON document.
    import base64

    decoded = base64.b64decode(captured["data"]["content"]).decode("utf-8")
    assert json.loads(decoded)["resource_id"] == "vpc-abc12345"


def test_post_slack_swallows_failure(monkeypatch):
    """Slack failures must never propagate — Lambda still returns 200."""
    def _boom(*_a, **_kw):
        raise OSError("boom")

    monkeypatch.setattr(handler.urllib.request, "urlopen", _boom)
    handler._post_slack("https://example", {
        "resource_type": "aws_vpc",
        "resource_id": "vpc-1",
        "action": "detach",
        "severity": "high",
    })  # no exception
