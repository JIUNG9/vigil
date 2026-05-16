# Real-Time Claude Triggers via Slack Socket Mode (No Public URL, No Ingress)

**Tags:** `Slack` `WebSocket` `Kubernetes` `AI Agents` `Real-Time`

---

> Part 2 of "Building teammate." How we replaced 15-minute cron polling with sub-second Slack message triggers, without exposing a single public endpoint. The architecture, the failure modes, and the strict-quiet notification model we landed on.

---

## The Problem

In Part 1, I described teammate's 11 agent routines — weekly digest, orphan triage, Jira sync, Confluence sync, etc. — each running as a Kubernetes CronJob. Cron was fine for scheduled work, but it had two failure modes that became increasingly painful:

1. **Stale state for hours**. Brain docs got pushed at 09:01, but the next routine didn't run until 09:15. For 14 minutes, the agent answered with yesterday's index.
2. **No on-demand path**. An engineer wanted to trigger a digest right now? Their options were: wait for cron, ssh to the cluster and `kubectl create job --from=cronjob`, or open a PR to bump the schedule.

Both were unacceptable for a tool that's supposed to **be in the loop**, not in the way.

I wanted: an engineer types `brain pulse` in Slack, and within 1 second a Kubernetes Job is running. No public URL, no ALB, no ingress rule, no kubectl in someone's terminal history.

---

## What I Considered

There are four ways to receive a Slack event in 2026:

| Pattern | Direction | Public URL needed? | Latency |
|---|---|---|---|
| **Events API (webhook)** | Slack → your cluster | yes (HTTPS reachable from internet) | ~1 s |
| **Outbound webhooks** (legacy) | Slack → your cluster | yes | ~1 s |
| **Polling `conversations.history`** | your cluster → Slack | no | minutes |
| **Socket Mode** (WebSocket) | your cluster → Slack | **no** | <1 s |

Events API is the default everyone reaches for. It also requires you to:

- Allocate a public DNS name (we'd need `slack.platform.example.com`)
- Stand up an Ingress with TLS (cert-manager or ACM)
- Add the endpoint to an ALB or NLB
- Solve the request-signing verification
- Decide which network policies allow Slack's source IPs through

That's a half-day of YAML and a permanent attack surface to babysit. For a feature that's "agent listens for keywords in Slack," it's overengineered.

Socket Mode flips the direction. The cluster opens an **outbound** WebSocket to `wss-primary.slack.com`, and Slack pushes events down that connection. Nothing inbound. No public URL. No TLS termination, no ALB, no ingress.

The trade-off: Socket Mode is **single-connection per app token**. You can't horizontally scale the listener — only one connection at a time. For most teams, that's fine; one pod handles thousands of events per second.

I picked Socket Mode.

---

## The Slack App, Minimally Configured

The configuration is a few clicks at `api.slack.com/apps`:

1. **Create app** → from scratch, pick your workspace
2. **OAuth & Permissions → Bot Token Scopes**: `channels:history`, `groups:history`, `channels:read`, `groups:read`, `chat:write`, `reactions:write`
3. **Socket Mode → Enable Socket Mode** → generate App-Level Token with scope `connections:write`
   - This is the `xapp-1-...` token
   - **Slack only shows it once**, so copy it immediately
4. **Event Subscriptions → Enable Events** → subscribe to `message.channels` and `message.groups`
5. **Install to Workspace** → grants the Bot User OAuth Token (`xoxb-...`)
6. In Slack, **invite the bot** to the channel(s) you want it to watch

Total config time: ~5 minutes. The tokens go into AWS Secrets Manager and sync to a Kubernetes Secret via External Secrets Operator.

---

## The Listener, in Python

The core listener is ~300 lines. Here's the shape:

```python
def run(poll_interval: int = 60, fail_on_disconnect: bool = True) -> int:
    """Open Slack Socket Mode WebSocket and listen for events."""

    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    app_token = os.environ["SLACK_APP_TOKEN"]   # xapp-...
    bot_token = os.environ["SLACK_BOT_TOKEN"]   # xoxb-...
    watch_channels = os.environ.get(
        "TEAMMATE_SLACK_CHANNELS", ""
    ).split(",")

    web_client = WebClient(token=bot_token)
    channel_ids = _resolve_channel_ids(web_client, watch_channels)

    # Heartbeat thread — writes /tmp/teammate-heartbeat every 30s
    # The K8s liveness probe checks this file's age.
    stop_event = threading.Event()
    threading.Thread(
        target=_heartbeat_thread, args=(stop_event,), daemon=True
    ).start()

    # Background polling thread for Jira/Confluence (no Socket Mode equivalent)
    threading.Thread(
        target=_poll_atlassian, args=(poll_interval, stop_event, {}),
        daemon=True,
    ).start()

    reconnect_attempts = 0
    initial_connect_announced = False

    def _handle(client, req):
        nonlocal reconnect_attempts
        reconnect_attempts = 0  # reset backoff on any event

        # Slack requires you to ack the envelope, even if you ignore the content
        client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        if req.type != "events_api":
            return
        event = req.payload.get("event", {})
        if event.get("type") != "message" or event.get("subtype"):
            return
        if channel_ids and event.get("channel") not in channel_ids:
            return

        routine = _route_message(event.get("text", ""))
        if routine:
            # Fire-and-forget — quiet mode, no per-Job Slack chatter.
            _create_k8s_job(routine, source="slack-socket")

    while True:
        try:
            sm_client = SocketModeClient(
                app_token=app_token, web_client=web_client
            )
            sm_client.socket_mode_request_listeners.append(_handle)
            sm_client.connect()
            Path("/tmp/teammate-ready").write_text("ok")
            if not initial_connect_announced:
                _notify_lifecycle(
                    f":white_check_mark: listener connected — "
                    f"watching `{', '.join(watch_channels)}`"
                )
                initial_connect_announced = True

            # Block — SocketModeClient runs on background threads
            while sm_client.is_connected():
                time.sleep(5)

        except Exception as exc:
            log.error("Socket Mode error: %s", exc)

        reconnect_attempts += 1
        if reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
            _notify_lifecycle(
                f":x: listener disconnected after "
                f"{reconnect_attempts} attempts. Pod restarting."
            )
            if fail_on_disconnect:
                return 1

        backoff = min(2 ** reconnect_attempts, 60)
        log.info("Reconnecting in %ds...", backoff)
        time.sleep(backoff)
```

The keyword router is dead simple:

```python
KEYWORD_ROUTES = {
    "weekly_digest":   ["weekly digest"],
    "orphan_triage":   ["orphan triage", "orphan"],
    "confluence_sync": ["confluence sync"],
    "jira_sync":       ["jira sync"],
    "brain_pulse":     ["brain pulse", "brain_pulse", "reindex"],
    "auto_pr_drafter": ["pr draft"],
}

def _route_message(text: str) -> str | None:
    lower = text.lower()
    for routine, keywords in KEYWORD_ROUTES.items():
        if any(kw in lower for kw in keywords):
            return routine
    return None
```

Substring match, lowercase. An engineer typing *"can someone do a brain pulse please?"* still triggers `brain_pulse`.

---

## Creating Kubernetes Jobs from Slack Messages

When a message matches, we don't run the routine in the listener pod (the listener should be idle 99% of the time and not block for minutes). We create a Kubernetes Job from a CronJob template:

```python
def _create_k8s_job(routine: str, source: str = "slack-socket") -> str | None:
    from kubernetes import client as k8s_client, config as k8s_config

    k8s_config.load_incluster_config()
    namespace = os.environ.get("TEAMMATE_NAMESPACE", "teammate-agent")
    cronjob_name = f"teammate-{routine.replace('_', '-')}"
    job_name = f"{routine.replace('_', '-')}-{source}-{int(time.time())}"

    batch = k8s_client.BatchV1Api()
    cj = batch.read_namespaced_cron_job(cronjob_name, namespace)

    # Respect concurrencyPolicy: Forbid — skip if already running.
    jobs = batch.list_namespaced_job(
        namespace,
        label_selector=f"teammate-routine={routine}",
    )
    active = [j for j in jobs.items if j.status.active and j.status.active > 0]
    if active:
        log.info("routine %s already running — skipping", routine)
        return None

    job_spec = cj.spec.job_template.spec
    job = k8s_client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=k8s_client.V1ObjectMeta(
            name=job_name,
            namespace=namespace,
            labels={"teammate-routine": routine, "triggered-by": source},
        ),
        spec=job_spec,
    )
    batch.create_namespaced_job(namespace, job)
    return job_name
```

The Job inherits everything from the CronJob template: init containers, env vars, volume mounts, resources, service account. The listener only needs `batch/cronjobs:get` and `batch/jobs:list,create` permissions — minimal RBAC blast radius.

The **label-selector active-job check** is the dedup primitive. If two engineers type `brain pulse` within the same second, the second request finds the first one's Job in the active set and exits. No new Job is created. No Redis, no Postgres advisory lock, no Zookeeper — Kubernetes is the lock.

---

## The Deployment Manifest

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: teammate-event-listener
  namespace: teammate-agent
spec:
  replicas: 1
  strategy:
    type: Recreate   # only one socket connection at a time
  selector:
    matchLabels: {app: teammate-event-listener}
  template:
    metadata:
      labels: {app: teammate-event-listener}
    spec:
      serviceAccountName: teammate-agent
      containers:
      - name: teammate-listener
        image: your-registry/teammate:latest
        imagePullPolicy: Always
        command:
          - teammate
          - agent
          - listen
          - --poll-interval
          - "60"
          - --fail-on-disconnect
        env:
          - name: SLACK_APP_TOKEN
            valueFrom:
              secretKeyRef:
                name: teammate-credentials
                key: slack-app-token
                optional: true
          - name: SLACK_BOT_TOKEN
            valueFrom:
              secretKeyRef:
                name: teammate-credentials
                key: slack-bot-token
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
        readinessProbe:
          exec:
            command: ["/bin/sh", "-c", "test -f /tmp/teammate-ready"]
          initialDelaySeconds: 30
          periodSeconds: 10
        resources:
          requests: {cpu: 100m, memory: 256Mi}
          limits:   {cpu: 500m, memory: 512Mi}
```

Three things worth highlighting:

### `strategy: Recreate`

Slack permits exactly one Socket Mode connection per app token. A rolling deployment would briefly have two pods active, two connections fighting, undefined behavior. `Recreate` guarantees the old pod is gone before the new one starts.

### Liveness probe = heartbeat file age

The listener writes `/tmp/teammate-heartbeat` every 30 seconds while the socket is alive. The probe fails if the file is more than 90 seconds stale. This catches the failure mode where the process is up but the socket is silently dead — the SDK doesn't always raise on TCP-level disconnects.

When the probe fails, Kubernetes restarts the pod, which reconnects. Total recovery time: ~45 seconds worst case.

### `slack-app-token` marked `optional: true`

I shipped the manifest before generating the App-Level Token in Slack. Without `optional: true`, the pod would fail to start with `CreateContainerConfigError`. With it, the pod starts, the listener exits with a clear log line — *"SLACK_APP_TOKEN (xapp-…) required"* — and Kubernetes restarts in a loop until the token lands in Secrets Manager. Much easier to debug.

---

## The Notification Model: From Verbose to Strict-Quiet

This is the part I got wrong twice before getting right.

### Iteration 1: Verbose (the obvious one)

Every Slack trigger got three responses:
1. ✅ reaction on the trigger message
2. Thread reply when the Job started: *"Running brain_pulse…"*
3. Thread reply when the Job finished: *"✅ brain_pulse completed in 12s"*

This was fine for the first day. By day three, the `#devops` channel was 60% bot replies. Engineers tuned them out, including the failure replies.

### Iteration 2: Errors-only

Drop the success replies, keep failures with a log tail. This was better, but still noisy because the Jira/Confluence polling threads emitted false-positive failures every few hours (Atlassian transient 5xx).

### Iteration 3: Strict-quiet (where we landed)

Drop all per-job chatter. Only two notifications fire, ever:

```python
def _notify_lifecycle(text: str) -> None:
    """Post a single connect/disconnect lifecycle line."""
    if not notify_channel:
        return
    with contextlib.suppress(Exception):
        web_client.chat_postMessage(channel=notify_channel, text=text)

# Fired ONCE per pod lifetime, on first successful Socket Mode connect:
_notify_lifecycle(
    f":white_check_mark: listener connected — "
    f"watching `{', '.join(watch_channels)}`"
)

# Fired ONCE before the process exits, after exhausting reconnect attempts:
_notify_lifecycle(
    f":x: listener disconnected after "
    f"{reconnect_attempts} attempts. Pod restarting."
)
```

That's it. No reactions, no thread replies, no per-job pings. The channel stays human.

The signal is in two other places:
- **kubectl logs** for engineers who care about per-job detail
- **Daily digest** at 09:00 — a single summary message with everything that fired

This is the most counter-intuitive lesson from the build: **for a tool engineers live with, fewer notifications is more trust**. A bot that pings 50 times a day is invisible by week two. A bot that pings only when it dies stays meaningful.

---

## Concurrency, Concretely

Three scenarios, three layers of dedup, all already shipped:

**Scenario A: Two engineers type "brain pulse" within the same second.**

The first request reaches `_create_k8s_job`, finds no active Job, creates one. The second request reaches `_create_k8s_job` ~50 ms later, finds the first Job's active status, returns `None`. Single Job runs. K8s API is the lock — no Redis needed.

**Scenario B: A CronJob fires at 09:00 and an engineer types the keyword at 09:00:02.**

CronJob has `concurrencyPolicy: Forbid`. The label-selector check in `_create_k8s_job` sees the active CronJob-spawned Job, returns `None`. Single Job runs.

**Scenario C: A pod restart happens during a long-running Job.**

The new pod starts, sees no active Socket Mode listener events to handle, eventually reconnects to Slack. The in-flight Job is unaffected — it's a separate K8s resource on a different node. The listener doesn't care about Job state once it's created. Fire-and-forget.

---

## Performance

Numbers from the running system:

| Metric | Value |
|---|---|
| End-to-end latency (Slack send → Job created) | 800-1200 ms |
| Pod CPU at idle | <5 m |
| Pod memory at idle | ~120 MB |
| Pod restarts (last 30d) | 8 (all scheduled, mostly Slack session rotation) |
| Reconnect events (last 30d) | 87 (Slack rotates sessions every few hours) |
| False-positive job creations | 0 |
| Engineer complaints about noise | 0 (post strict-quiet) |
| Engineer complaints about silence | 0 |

Slack rotates the underlying Socket Mode session every few hours — this is by design, not a bug. The SDK handles it transparently if you have the reconnect loop wired correctly.

---

## What I'd Do Differently

1. **Skip the verbose notification iteration.** Start strict-quiet from day 1. The "let users see everything" instinct is wrong here.

2. **Make `SLACK_APP_TOKEN` optional from day 1.** Don't gate the pod on a token that requires a manual Slack admin step.

3. **Resolve channel IDs once at startup, not per-event.** I did this from the start, but the temptation to call `conversations.list` per event is real and would have been a rate-limit disaster.

4. **Add a `/teammate dry-run` Slack slash command** that prints what *would* happen without creating a Job. Useful for debugging keyword regex. (Not yet shipped — on the v2 roadmap.)

---

## Try It Yourself

```bash
pip install 'claude-teammate[listen]'

# After generating xapp- and xoxb- tokens at api.slack.com/apps:
export SLACK_APP_TOKEN="xapp-..."
export SLACK_BOT_TOKEN="xoxb-..."
export TEAMMATE_SLACK_CHANNELS="devops"

teammate agent listen --no-fail-on-disconnect
```

Then type `brain pulse` in `#devops`. The listener log will show:

```
INFO  Slack Socket Mode connected (watching: devops)
INFO  Slack → routine=brain_pulse text='brain pulse'
INFO  created Job teammate-agent/brain-pulse-slack-socket-1747012345
```

Full Kubernetes Deployment manifest: https://github.com/JIUNG9/teammate/blob/main/examples/k8s/event-listener/deployment.yaml

Source code: https://github.com/JIUNG9/teammate/blob/main/src/teammate/socket_listener.py

---

*Part 2 of "Building teammate." [← Part 1: Local-first brain](./01-local-first-brain.md) · [Next: Importing 25,000 docs from 4 sources →](./03-importers-25k-docs.md)*
