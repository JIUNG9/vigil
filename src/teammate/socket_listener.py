"""Slack Socket Mode event listener — real-time event bridge.

Opens a persistent WebSocket to Slack via Socket Mode (no public URL needed).
On relevant events, creates Kubernetes Jobs for teammate routines.
Also polls Jira/Confluence on a configurable interval for changes.

Required env vars:
  SLACK_APP_TOKEN    xapp-... (App-Level Token, Socket Mode > connections:write)
  SLACK_BOT_TOKEN    xoxb-... (Bot Token)

Optional env vars:
  TEAMMATE_SLACK_CHANNELS    comma-separated channel names to watch (default: all)
  TEAMMATE_NAMESPACE         K8s namespace for Job creation (default: teammate-agent)
  ATLASSIAN_API_TOKEN        enables Jira/Confluence polling if set
  ATLASSIAN_EMAIL            email for Atlassian Basic auth
  JIRA_BASE_URL              e.g. https://your-org.atlassian.net
  CONFLUENCE_BASE_URL        e.g. https://your-org.atlassian.net/wiki
  JIRA_WATCHER_JQL           JQL for issues that should trigger jira_sync
  CONFLUENCE_WATCHER_SPACES  comma-separated space keys to watch (e.g. "DOCS,ENG")

Fail-fast behavior:
  - Writes /tmp/teammate-heartbeat every 30s while socket is alive
  - Writes /tmp/teammate-ready once fully connected
  - Exits non-zero if reconnect fails after MAX_RECONNECT_ATTEMPTS
    (k8s liveness probe detects stale heartbeat → pod restart → reconnect)
"""

from __future__ import annotations

import logging
import os
import time
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Routing: Slack message text → routine name
# ---------------------------------------------------------------------------

KEYWORD_ROUTES: dict[str, list[str]] = {
    "weekly_digest":    ["weekly digest"],
    "orphan_triage":    ["orphan triage", "orphan"],
    "confluence_sync":  ["confluence sync"],
    "jira_sync":        ["jira sync"],
    "auto_pr_drafter":  ["pr draft"],
    "brain_pulse":      ["brain pulse", "reindex"],
}


def _route_message(text: str) -> str | None:
    """Return routine name if message matches a keyword, else None."""
    lower = text.lower()
    for routine, keywords in KEYWORD_ROUTES.items():
        if any(kw in lower for kw in keywords):
            return routine
    return None


# ---------------------------------------------------------------------------
# Kubernetes Job creation
# ---------------------------------------------------------------------------

def _create_k8s_job(routine: str, source: str = "slack-socket") -> bool:
    """Create a K8s Job from the CronJob template for the given routine."""
    try:
        from kubernetes import client as k8s_client, config as k8s_config
    except ImportError:
        log.error("kubernetes package not installed — cannot create Job")
        return False

    try:
        k8s_config.load_incluster_config()
    except Exception:
        try:
            k8s_config.load_kube_config()
        except Exception as exc:
            log.error("cannot load kubeconfig: %s", exc)
            return False

    namespace = os.environ.get("TEAMMATE_NAMESPACE", "teammate-agent")
    cronjob_name = f"teammate-{routine.replace('_', '-')}"
    job_name = f"{routine.replace('_', '-')}-{source}-{int(time.time())}"

    batch = k8s_client.BatchV1Api()

    try:
        cj = batch.read_namespaced_cron_job(cronjob_name, namespace)
    except Exception as exc:
        log.error("cannot read CronJob %s: %s", cronjob_name, exc)
        return False

    # Respect concurrencyPolicy: Forbid — skip if already running
    try:
        jobs = batch.list_namespaced_job(
            namespace,
            label_selector=f"teammate-routine={routine}",
        )
        active = [j for j in jobs.items if j.status.active and j.status.active > 0]
        if active:
            log.info("routine %s already running — skipping", routine)
            return False
    except Exception as exc:
        log.warning("cannot check active jobs: %s", exc)

    job_spec = cj.spec.job_template.spec
    labels = {"teammate-routine": routine, "triggered-by": source}
    job = k8s_client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=k8s_client.V1ObjectMeta(name=job_name, namespace=namespace, labels=labels),
        spec=job_spec,
    )
    try:
        batch.create_namespaced_job(namespace, job)
        log.info("created Job %s/%s (routine=%s source=%s)", namespace, job_name, routine, source)
        return True
    except Exception as exc:
        log.error("failed to create Job: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Heartbeat thread (liveness probe support)
# ---------------------------------------------------------------------------

def _heartbeat_thread(stop: threading.Event) -> None:
    heartbeat_path = Path("/tmp/teammate-heartbeat")
    while not stop.is_set():
        heartbeat_path.write_text(datetime.now(timezone.utc).isoformat())
        stop.wait(30)


# ---------------------------------------------------------------------------
# Jira / Confluence polling thread
# ---------------------------------------------------------------------------

def _poll_jira_confluence(interval: int, stop: threading.Event, last_seen: dict) -> None:
    """Poll Jira/Confluence every `interval` seconds; create Jobs on new items."""
    atlassian_token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    if not atlassian_token:
        log.info("ATLASSIAN_API_TOKEN not set — Jira/Confluence polling disabled")
        return

    try:
        import httpx
    except ImportError:
        log.warning("httpx not installed — Jira/Confluence polling disabled")
        return

    jira_url = os.environ.get("JIRA_BASE_URL", "")
    conf_url = os.environ.get("CONFLUENCE_BASE_URL", "")
    jira_email = os.environ.get("ATLASSIAN_EMAIL", "")
    jira_jql = os.environ.get(
        "JIRA_WATCHER_JQL",
        'labels = "architecture-decision" AND updated > -2m',
    )
    confluence_spaces_raw = os.environ.get("CONFLUENCE_WATCHER_SPACES", "")
    confluence_spaces = [s.strip() for s in confluence_spaces_raw.split(",") if s.strip()]

    auth = (jira_email, atlassian_token)

    while not stop.is_set():
        stop.wait(interval)
        if stop.is_set():
            break

        if jira_url:
            try:
                resp = httpx.get(
                    f"{jira_url}/rest/api/3/search",
                    params={"jql": jira_jql, "maxResults": 5},
                    auth=auth,
                    timeout=10,
                )
                if resp.status_code == 200:
                    for issue in resp.json().get("issues", []):
                        issue_id = issue["id"]
                        if issue_id not in last_seen.get("jira", set()):
                            last_seen.setdefault("jira", set()).add(issue_id)
                            log.info("Jira: new issue %s — triggering jira_sync", issue["key"])
                            _create_k8s_job("jira_sync", source="jira-poll")
                            break
            except Exception as exc:
                log.warning("Jira poll error: %s", exc)

        if conf_url and confluence_spaces:
            try:
                since = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )
                spaces_cql = ", ".join(f'"{s}"' for s in confluence_spaces)
                resp = httpx.get(
                    f"{conf_url}/rest/api/content/search",
                    params={
                        "cql": f'space IN ({spaces_cql}) AND lastModified > "{since}"',
                        "limit": 5,
                    },
                    auth=auth,
                    timeout=10,
                )
                if resp.status_code == 200:
                    for page in resp.json().get("results", []):
                        page_id = page["id"]
                        if page_id not in last_seen.get("confluence", set()):
                            last_seen.setdefault("confluence", set()).add(page_id)
                            log.info("Confluence: new page %r — triggering confluence_sync", page["title"])
                            _create_k8s_job("confluence_sync", source="confluence-poll")
                            break
            except Exception as exc:
                log.warning("Confluence poll error: %s", exc)


# ---------------------------------------------------------------------------
# Slack Socket Mode listener (main entry point)
# ---------------------------------------------------------------------------

MAX_RECONNECT_ATTEMPTS = 5


def run(poll_interval: int = 60, fail_on_disconnect: bool = True) -> int:
    """Open Slack Socket Mode WebSocket and listen for events.

    Returns exit code: 0 = clean stop, 1 = fatal disconnect.
    """
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk import WebClient
    except ImportError:
        log.error("slack-sdk not installed. Run: pip install 'claude-teammate[listen]'")
        return 1

    app_token = os.environ.get("SLACK_APP_TOKEN", "")   # xapp-...
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")   # xoxb-...
    channels_raw = os.environ.get("TEAMMATE_SLACK_CHANNELS", "")
    watch_channels = [c.strip() for c in channels_raw.split(",") if c.strip()]

    if not app_token or not app_token.startswith("xapp-"):
        log.error("SLACK_APP_TOKEN (xapp-...) required. "
                  "Enable Socket Mode in your Slack app and generate an App-Level Token.")
        return 1
    if not bot_token:
        log.error("SLACK_BOT_TOKEN (xoxb-...) required")
        return 1

    web_client = WebClient(token=bot_token)

    channel_ids: set[str] = set()
    if watch_channels:
        try:
            for page in web_client.conversations_list(types="public_channel,private_channel"):
                for ch in page["channels"]:
                    if any(ch["name"].lstrip("#") == w.lstrip("#") for w in watch_channels):
                        channel_ids.add(ch["id"])
        except Exception as exc:
            log.warning("cannot resolve channel IDs: %s — will watch all channels", exc)

    stop_event = threading.Event()

    hb_thread = threading.Thread(target=_heartbeat_thread, args=(stop_event,), daemon=True)
    hb_thread.start()

    last_seen: dict = {}
    poll_thread = threading.Thread(
        target=_poll_jira_confluence,
        args=(poll_interval, stop_event, last_seen),
        daemon=True,
    )
    poll_thread.start()

    reconnect_attempts = 0

    def _handle(client: SocketModeClient, req: SocketModeRequest) -> None:
        nonlocal reconnect_attempts
        reconnect_attempts = 0

        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        if req.type != "events_api":
            return
        event = req.payload.get("event", {})
        if event.get("type") != "message" or event.get("subtype"):
            return
        if channel_ids and event.get("channel") not in channel_ids:
            return

        text = event.get("text", "")
        routine = _route_message(text)
        if routine:
            log.info("Slack → routine=%s text=%r", routine, text[:80])
            created = _create_k8s_job(routine, source="slack-socket")
            if created:
                try:
                    web_client.reactions_add(
                        channel=event["channel"],
                        timestamp=event["ts"],
                        name="white_check_mark",
                    )
                except Exception:
                    pass

    while True:
        try:
            sm_client = SocketModeClient(app_token=app_token, web_client=web_client)
            sm_client.socket_mode_request_listeners.append(_handle)
            sm_client.connect()
            Path("/tmp/teammate-ready").write_text("ok")
            scope = ", ".join(watch_channels) if watch_channels else "all channels"
            log.info("Slack Socket Mode connected (watching: %s)", scope)
            reconnect_attempts = 0

            while sm_client.is_connected():
                time.sleep(5)

            log.warning("Slack socket disconnected")

        except Exception as exc:
            log.error("Socket Mode error: %s", exc)

        reconnect_attempts += 1
        if reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
            log.error("Socket Mode failed %d times — giving up", reconnect_attempts)
            stop_event.set()
            return 1 if fail_on_disconnect else 0

        backoff = min(2 ** reconnect_attempts, 60)
        log.info("Reconnecting in %ds (attempt %d/%d)…", backoff, reconnect_attempts, MAX_RECONNECT_ATTEMPTS)
        time.sleep(backoff)
