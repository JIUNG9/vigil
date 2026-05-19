"""Layer 1 — rule layer.

Watchlist rules are authored in YAML in the brain repo (`watchlist/*.yaml`)
and synced to the alerting backend (SigNoz, Grafana, Prometheus rules, etc.).

This module:
- Parses watchlist YAML
- Diffs against the alerting backend's current rules
- Applies adds/removes/updates via the backend's API
- Reports drift

It runs as a CronJob, idempotent. Engineers PR the YAML; the syncer
reconciles automatically on next run.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class WatchRule:
    name: str
    expr: str
    duration: str        # e.g. "5m"
    severity: str        # critical / high / medium / low
    owner: str           # e.g. "@oncall-devsecops"
    runbooks: list[str]
    description: str = ""


class RuleLayer:
    """Watchlist YAML → alerting-backend rules."""

    def __init__(self, watchlist_dir: Path | None = None):
        brain = os.environ.get("TEAMMATE_BRAIN_ROOT", ".")
        self.dir = Path(watchlist_dir or f"{brain}/watchlist")

    def load_rules(self) -> list[WatchRule]:
        try:
            import yaml
        except ImportError:
            raise RuntimeError("PyYAML required: pip install 'claude-vigil[mttd]'")

        rules: list[WatchRule] = []
        if not self.dir.exists():
            return rules
        for f in sorted(self.dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or []
            except yaml.YAMLError as exc:
                log.warning("invalid YAML in %s: %s", f, exc)
                continue
            for entry in data:
                rules.append(WatchRule(
                    name=entry.get("alert", ""),
                    expr=entry.get("expr", ""),
                    duration=entry.get("for", "5m"),
                    severity=entry.get("severity", "medium"),
                    owner=entry.get("owner", "@oncall"),
                    runbooks=entry.get("runbooks", []),
                    description=entry.get("description", ""),
                ))
        return rules

    def sync_to_signoz(self, rules: list[WatchRule]) -> dict:
        """Reconcile rules to SigNoz alert API. Returns counts (added/updated/removed)."""
        import httpx

        base = os.environ.get("SIGNOZ_API_URL", "")
        token = os.environ.get("SIGNOZ_API_TOKEN", "")
        if not base or not token:
            log.warning("SIGNOZ_API_URL / SIGNOZ_API_TOKEN not set — skipping sync")
            return {"added": 0, "updated": 0, "removed": 0, "skipped": True}

        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(headers=headers, timeout=30) as client:
            # Fetch current rules
            r = client.get(f"{base}/api/v1/rules")
            r.raise_for_status()
            current = {rule["alert"]: rule for rule in r.json().get("data", {}).get("rules", [])}

            desired = {rule.name: rule for rule in rules}
            added = updated = removed = 0

            for name, rule in desired.items():
                payload = {
                    "alert": rule.name,
                    "expr": rule.expr,
                    "for": rule.duration,
                    "labels": {"severity": rule.severity, "owner": rule.owner},
                    "annotations": {
                        "summary": rule.name,
                        "description": rule.description,
                        "runbooks": ", ".join(rule.runbooks),
                    },
                }
                if name not in current:
                    r = client.post(f"{base}/api/v1/rules", json=payload)
                    if r.status_code in (200, 201):
                        added += 1
                else:
                    rid = current[name].get("id")
                    if rid:
                        r = client.put(f"{base}/api/v1/rules/{rid}", json=payload)
                        if r.status_code == 200:
                            updated += 1

            # Remove rules in SigNoz no longer present in YAML
            for name, rule in current.items():
                if name not in desired and rule.get("source") == "vigil":
                    rid = rule.get("id")
                    if rid:
                        r = client.delete(f"{base}/api/v1/rules/{rid}")
                        if r.status_code in (200, 204):
                            removed += 1

        log.info("sync: added=%d updated=%d removed=%d", added, updated, removed)
        return {"added": added, "updated": updated, "removed": removed, "skipped": False}


def main() -> int:
    """CLI entry: vigil mttd sync-watchlist"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    layer = RuleLayer()
    rules = layer.load_rules()
    log.info("loaded %d watchlist rules", len(rules))
    out = layer.sync_to_signoz(rules)
    print(f"watchlist sync: {out}")
    return 0
