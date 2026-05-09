"""AWS Lambda handler — CloudTrail event → brain-invalidations commit.

Triggered by EventBridge whenever CloudTrail emits one of the configured
event names (``DetachVpcCidrBlock``, ``DeleteRole``, ``ModifyDBInstance``,
…). Maps the event to the team-brain ``InvalidationEvent`` shape and
commits the JSON file to the brain-invalidations GitHub repo via the
GitHub Contents API.

This file is the *runtime artifact* — it has zero imports beyond the
Python stdlib (``urllib`` for HTTP, ``boto3`` for SSM). The terraform
module deploys it; the tests in ``tests/`` exercise it with mocks.

Environment::

  GITHUB_REPO              = "your-org/brain-invalidations"
  GITHUB_BRANCH            = "main"
  GITHUB_PAT_SSM_PARAMETER = "/teammate/github_pat"
  SLACK_WEBHOOK_URL        = ""  # optional; if set, a notice is posted
  SEVERITY_MAP_JSON        = '{"DetachVpcCidrBlock":"high", ...}'
  DEFAULT_SEVERITY         = "medium"
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------- helpers ----------


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _slugify(value: str) -> str:
    out = []
    last_dash = False
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    return "".join(out).strip("-") or "resource"


def _severity_map() -> dict[str, str]:
    raw = os.environ.get("SEVERITY_MAP_JSON", "{}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("SEVERITY_MAP_JSON failed to parse — using defaults")
        return {}
    if isinstance(data, dict):
        return {str(k): str(v).lower() for k, v in data.items()}
    return {}


def _resolve_severity(event_name: str) -> str:
    smap = _severity_map()
    if event_name in smap and smap[event_name] in {"low", "medium", "high", "critical"}:
        return smap[event_name]
    return os.environ.get("DEFAULT_SEVERITY", "medium").lower()


# ---------- SSM parameter ----------


def _get_pat(parameter_name: str) -> str:
    """Read the GitHub PAT from SSM Parameter Store. Boto3 is loaded lazily
    so the unit tests don't need an AWS session.
    """
    import boto3  # noqa: PLC0415

    client = boto3.client("ssm")
    resp = client.get_parameter(Name=parameter_name, WithDecryption=True)
    return resp["Parameter"]["Value"]


# ---------- mapping ----------


_DEFAULT_ACTION_BY_EVENT: dict[str, str] = {
    "DetachVpcCidrBlock": "detach",
    "AssociateVpcCidrBlock": "modify",
    "DeleteVpc": "delete",
    "ModifyVpcAttribute": "modify",
    "DeleteRole": "delete",
    "DetachRolePolicy": "modify",
    "PutRolePolicy": "modify",
    "DeleteSecurityGroup": "delete",
    "AuthorizeSecurityGroupIngress": "modify",
    "RevokeSecurityGroupIngress": "modify",
    "ModifyDBInstance": "modify",
    "DeleteDBInstance": "delete",
    "CreateDBClusterSnapshot": "create",
    "DeleteDBClusterSnapshot": "delete",
}


def _resource_from_request(detail: dict[str, Any]) -> tuple[str, str]:
    """Best-effort extraction of (resource_type, resource_id) from a CloudTrail
    detail block. The CloudTrail event schema is per-API-call shaped, so we
    grab the most common fields and let ``resource_type`` fall back to the
    AWS source service.
    """
    request = detail.get("requestParameters") or {}
    response = detail.get("responseElements") or {}
    event_source = str(detail.get("eventSource", "")).split(".")[0]
    rtype = f"aws_{event_source}" if event_source else "aws_resource"

    # Common id keys, in priority order.
    for key in (
        "vpcId", "subnetId", "securityGroupId", "instanceId",
        "dBInstanceIdentifier", "dBClusterIdentifier",
        "roleName", "policyArn", "userName", "groupName",
    ):
        for src in (request, response):
            value = src.get(key)
            if isinstance(value, str) and value:
                return rtype, value
    # ARN fallback
    for key in ("resources", "resource"):
        value = detail.get(key)
        if isinstance(value, list) and value:
            arn = value[0].get("ARN") if isinstance(value[0], dict) else value[0]
            if arn:
                return rtype, str(arn)
    return rtype, "unknown"


def map_event(event: dict[str, Any]) -> dict[str, Any]:
    """Translate an EventBridge → CloudTrail wrapped event into the brain
    ``InvalidationEvent`` shape (a plain dict — no dataclasses in Lambda).
    """
    detail = event.get("detail") or {}
    event_name = str(detail.get("eventName", ""))
    rtype, rid = _resource_from_request(detail)
    severity = _resolve_severity(event_name)
    action = _DEFAULT_ACTION_BY_EVENT.get(event_name, "modify")

    actor = ""
    user_identity = detail.get("userIdentity") or {}
    if isinstance(user_identity, dict):
        actor = (
            user_identity.get("arn")
            or user_identity.get("userName")
            or user_identity.get("type")
            or ""
        )

    return {
        "id": uuid.uuid4().hex,
        "timestamp": str(detail.get("eventTime") or _now_iso()),
        "source": "cloudtrail",
        "resource_type": rtype,
        "resource_id": rid,
        "action": action,
        "severity": severity,
        "actor": actor,
        "metadata": {
            "event_name": event_name,
            "aws_region": detail.get("awsRegion", ""),
            "account": str(detail.get("recipientAccountId", "")),
        },
    }


# ---------- GitHub commit ----------


def _commit_to_github(
    event_dict: dict[str, Any],
    repo: str,
    branch: str,
    pat: str,
) -> dict[str, Any]:
    """Commit the JSON event to the GitHub Contents API.

    Pure stdlib — keeps the Lambda zip tiny. Returns the GitHub response
    body (parsed JSON) so callers can log the commit SHA.
    """
    ts = event_dict.get("timestamp") or _now_iso()
    try:
        ts_dt = _dt.datetime.fromisoformat(ts)
    except ValueError:
        ts_dt = _dt.datetime.now(_dt.UTC)

    slug = _slugify(f"{event_dict['resource_type']}-{event_dict['resource_id']}")
    fname = f"{slug}-{_slugify(event_dict['action'])}-{int(ts_dt.timestamp())}.json"
    path = (
        f"invalidations/{ts_dt.year:04d}/{ts_dt.month:02d}/{ts_dt.day:02d}/{fname}"
    )

    body = json.dumps(event_dict, indent=2, sort_keys=True) + "\n"
    payload = {
        "message": (
            f"chore(invalidations): {event_dict['action']} "
            f"{event_dict['resource_type']}.{event_dict['resource_id']} "
            f"({event_dict['severity']})"
        ),
        "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "teammate-cloudtrail-hook/0.9.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_slack(webhook_url: str, event_dict: dict[str, Any]) -> None:
    """Best-effort Slack notice. Failures never propagate."""
    payload = {
        "text": (
            f":warning: brain-invalidation: "
            f"{event_dict['resource_type']}.{event_dict['resource_id']} "
            f"{event_dict['action']} ({event_dict['severity'].upper()})"
        ),
    }
    req = urllib.request.Request(
        url=webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("slack post failed: %s", exc)


# ---------- entry point ----------


def lambda_handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """EventBridge → CloudTrail event handler.

    ``event`` is the EventBridge payload — typically a wrapped CloudTrail
    record under ``detail``. We map → commit → (optionally) Slack notify.
    """
    repo = os.environ["GITHUB_REPO"]
    branch = os.environ.get("GITHUB_BRANCH", "main")
    pat_param = os.environ["GITHUB_PAT_SSM_PARAMETER"]
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")

    mapped = map_event(event)
    logger.info("mapped event: %s", json.dumps(mapped, sort_keys=True))

    pat = _get_pat(pat_param)
    response = _commit_to_github(mapped, repo, branch, pat)
    sha = (response.get("commit") or {}).get("sha", "?")
    logger.info("github commit sha=%s path=%s",
                sha, (response.get("content") or {}).get("path"))

    if slack_url:
        _post_slack(slack_url, mapped)

    return {"status": "ok", "commit_sha": sha, "event": mapped}
