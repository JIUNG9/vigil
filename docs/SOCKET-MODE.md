# Slack Socket Mode — Real-time Event Listener

teammate v0.11 adds a persistent WebSocket listener that replaces cron-based polling
for Slack events. The listener runs as a single-replica Kubernetes Deployment and
triggers K8s Jobs for teammate routines **in real time** when a matching Slack message
arrives.

## How it works

```
Slack workspace
    │  WebSocket (outbound from cluster — no public URL needed)
    ▼
teammate-event-listener Deployment (replicas=1)
    │  creates K8s Job on matching message
    ▼
teammate-{routine} CronJob template
    │  runs teammate agent run <routine>
    ▼
brain repo / Slack / Jira / Confluence
```

The WebSocket is a **Slack Socket Mode** connection — the cluster initiates the
connection outbound. You do not need an ALB, public DNS, or an ingress rule.

Jira and Confluence changes are still detected via **polling** (default 60s), because
Atlassian webhooks require a publicly reachable URL.

---

## Prerequisites

1. **Slack app with Socket Mode enabled.**
   - Go to `api.slack.com/apps` → your app → **Socket Mode** → Enable
   - Under **App-Level Tokens** → **Generate New Token**
     - Name: e.g. `teammate-socket`
     - Scope: `connections:write`
   - Copy the `xapp-...` token — this is `SLACK_APP_TOKEN`

2. **Bot Token** (`xoxb-...`) — your existing `SLACK_BOT_TOKEN`. No new scopes needed
   beyond `channels:read`, `reactions:write`, and `chat:write`.

3. **Event subscriptions** (in Slack app settings → **Event Subscriptions**):
   - Enable events
   - Subscribe to bot events: `message.channels`, `message.groups`

---

## Local setup and testing

```bash
# Install with listen extras
pip install 'claude-teammate[listen]'

# Set tokens (never commit these)
export SLACK_APP_TOKEN="xapp-..."
export SLACK_BOT_TOKEN="xoxb-..."
export TEAMMATE_SLACK_CHANNELS="ops-alerts"   # channel to watch

# Optional: Jira/Confluence polling
export ATLASSIAN_API_TOKEN="..."
export ATLASSIAN_EMAIL="you@example.com"
export JIRA_BASE_URL="https://your-org.atlassian.net"
export CONFLUENCE_BASE_URL="https://your-org.atlassian.net/wiki"
export CONFLUENCE_WATCHER_SPACES="DOCS,ENG"   # space keys to watch

# Start the listener (Ctrl-C to stop; --no-fail-on-disconnect keeps it running locally)
teammate agent listen --no-fail-on-disconnect

# Test it: say "brain pulse" in #ops-alerts and watch the log
```

Expected output when connected:
```
INFO  Slack Socket Mode connected (watching: ops-alerts)
```

When you send a matching message in the watched channel:
```
INFO  Slack → routine=brain_pulse text='brain pulse'
INFO  created Job teammate-agent/brain-pulse-slack-socket-1747012345 (routine=brain_pulse source=slack-socket)
```

---

## Keyword routes (default)

| Say in Slack | Triggers routine |
|---|---|
| `weekly digest` | `weekly_digest` |
| `orphan triage` / `orphan` | `orphan_triage` |
| `confluence sync` | `confluence_sync` |
| `jira sync` | `jira_sync` |
| `pr draft` | `auto_pr_drafter` |
| `brain pulse` / `reindex` | `brain_pulse` |

Routes are defined in `socket_listener.KEYWORD_ROUTES` — edit and rebuild to customize.

---

## Kubernetes Deployment

For production, run the listener as a Deployment with `replicas: 1` and
`strategy: Recreate` (only one WebSocket connection at a time).

See `examples/k8s/event-listener/` for complete manifests:
- `deployment.yaml` — the listener Deployment with liveness/readiness probes
- `externalsecret.yaml` — ESO SecretStore mapping for tokens
- `cronjob-brain-pulse.yaml` — fallback 15-min cron (catches webhook gaps)

### Liveness probe

The listener writes `/tmp/teammate-heartbeat` every 30s while the socket is alive.
The probe fails if the file is more than 90s old → pod restarts → reconnects.

```yaml
livenessProbe:
  exec:
    command:
      - /bin/sh
      - -c
      - |
        test -f /tmp/teammate-heartbeat && \
        [ $(( $(date +%s) - $(stat -c %Y /tmp/teammate-heartbeat) )) -lt 90 ]
  initialDelaySeconds: 60
  periodSeconds: 30
  failureThreshold: 2
```

### Fail-fast on disconnect

`--fail-on-disconnect` (the default) causes the process to exit with code 1 after
`MAX_RECONNECT_ATTEMPTS` (5) reconnect failures. Kubernetes restarts the pod, which
reconnects. Disable this flag (`--no-fail-on-disconnect`) for local development so
the process doesn't exit when Ctrl-C is pressed.

---

## What is NOT real-time

| Source | Latency | Mechanism |
|---|---|---|
| Slack messages | <1s | WebSocket (Socket Mode) |
| Brain docs pushed to git | ~30-60s | GitHub Actions webhook (see examples/k8s/) |
| Jira issue updates | ~60s | HTTP polling thread |
| Confluence page edits | ~60s | HTTP polling thread |

True Jira/Confluence real-time requires configuring webhooks from Atlassian to an
API Gateway endpoint. This is out of scope for the default setup.

---

## Environment variable reference

| Variable | Required | Description |
|---|---|---|
| `SLACK_APP_TOKEN` | yes | `xapp-...` App-Level Token (Socket Mode) |
| `SLACK_BOT_TOKEN` | yes | `xoxb-...` Bot Token |
| `TEAMMATE_SLACK_CHANNELS` | no | Comma-separated channel names. Empty = all channels. |
| `TEAMMATE_NAMESPACE` | no | K8s namespace for Job creation (default: `teammate-agent`) |
| `ATLASSIAN_API_TOKEN` | no | Enables Jira/Confluence polling |
| `ATLASSIAN_EMAIL` | no | Email for Atlassian Basic auth |
| `JIRA_BASE_URL` | no | e.g. `https://your-org.atlassian.net` |
| `CONFLUENCE_BASE_URL` | no | e.g. `https://your-org.atlassian.net/wiki` |
| `JIRA_WATCHER_JQL` | no | JQL filter for `jira_sync` triggers |
| `CONFLUENCE_WATCHER_SPACES` | no | Comma-separated Confluence space keys |
