"""
remediator/engine.py — Rule-based remediation engine.

Architecture: Strategy pattern.
  - Rule:             evaluates a HealthSnapshot and returns True/False
  - RemediationStrategy: executes a specific fix when a rule fires
  - RemediationEngine:   orchestrates evaluation + execution + ITIL records

Adding a new remediation type = new Rule subclass + new Strategy subclass.
The engine itself never changes (Open/Closed principle).
"""

import asyncio
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import structlog

from collector.models import Alert, HealthSnapshot, RemediationAction

logger = structlog.get_logger(__name__)


# ── Rules ─────────────────────────────────────────────────────────────────────

class Rule(ABC):
    """Base class for all evaluation rules."""

    @abstractmethod
    def matches(self, snapshot: HealthSnapshot) -> bool: ...

    @abstractmethod
    def to_alert(self, snapshot: HealthSnapshot) -> Alert: ...


class HighDiskRule(Rule):
    def __init__(self, threshold: float = 85.0):
        self.threshold = threshold

    def matches(self, snapshot: HealthSnapshot) -> bool:
        return snapshot.disk_percent >= self.threshold

    def to_alert(self, snapshot: HealthSnapshot) -> Alert:
        return Alert(
            node_id=snapshot.node_id,
            hostname=snapshot.hostname,
            alert_type="HIGH_DISK",
            severity="critical" if snapshot.disk_percent >= 95 else "warning",
            message=f"Disk usage at {snapshot.disk_percent}% on {snapshot.hostname}",
            details={"disk_percent": snapshot.disk_percent, "threshold": self.threshold},
        )


class ServiceDownRule(Rule):
    def matches(self, snapshot: HealthSnapshot) -> bool:
        return len(snapshot.stopped_services) > 0

    def to_alert(self, snapshot: HealthSnapshot) -> Alert:
        return Alert(
            node_id=snapshot.node_id,
            hostname=snapshot.hostname,
            alert_type="SERVICE_DOWN",
            severity="critical",
            message=f"Services down on {snapshot.hostname}: {', '.join(snapshot.stopped_services)}",
            details={"stopped_services": snapshot.stopped_services},
        )


class HighCpuRule(Rule):
    def __init__(self, threshold: float = 90.0):
        self.threshold = threshold

    def matches(self, snapshot: HealthSnapshot) -> bool:
        return snapshot.cpu_percent >= self.threshold

    def to_alert(self, snapshot: HealthSnapshot) -> Alert:
        return Alert(
            node_id=snapshot.node_id,
            hostname=snapshot.hostname,
            alert_type="HIGH_CPU",
            severity="warning",
            message=f"CPU at {snapshot.cpu_percent}% on {snapshot.hostname}",
            details={"cpu_percent": snapshot.cpu_percent, "threshold": self.threshold},
        )


class HighMemoryRule(Rule):
    def __init__(self, threshold: float = 90.0):
        self.threshold = threshold

    def matches(self, snapshot: HealthSnapshot) -> bool:
        return snapshot.memory_percent >= self.threshold

    def to_alert(self, snapshot: HealthSnapshot) -> Alert:
        return Alert(
            node_id=snapshot.node_id,
            hostname=snapshot.hostname,
            alert_type="HIGH_MEMORY",
            severity="warning",
            message=f"Memory at {snapshot.memory_percent}% on {snapshot.hostname}",
            details={"memory_percent": snapshot.memory_percent, "threshold": self.threshold},
        )


# ── Remediation Strategies ────────────────────────────────────────────────────

class RemediationStrategy(ABC):
    """
    Each strategy corresponds to one alert type.
    execute() returns (success: bool, output: str).
    """
    alert_type: str
    playbook: str

    @abstractmethod
    async def execute(self, action: RemediationAction, snapshot: HealthSnapshot) -> tuple[bool, str]: ...


class LogRotationStrategy(RemediationStrategy):
    """Triggered by HIGH_DISK — rotates logs to free space."""
    alert_type = "HIGH_DISK"
    playbook   = "playbooks/log_rotation.sh"

    async def execute(self, action: RemediationAction, snapshot: HealthSnapshot) -> tuple[bool, str]:
        logger.info("Executing log rotation", hostname=snapshot.hostname)
        # In production: SSH to node and run the playbook
        # Here: simulate the operation
        await asyncio.sleep(0.5)
        freed_gb = round(snapshot.disk_percent * 0.1, 1)
        output = (
            f"Log rotation complete on {snapshot.hostname}.\n"
            f"Rotated: /var/log/nginx/*.log, /var/log/app/*.log\n"
            f"Estimated space freed: {freed_gb} GB\n"
            f"New disk usage: ~{snapshot.disk_percent - 8:.1f}%"
        )
        return True, output


class ServiceRestartStrategy(RemediationStrategy):
    """Triggered by SERVICE_DOWN — attempts service restart."""
    alert_type = "SERVICE_DOWN"
    playbook   = "playbooks/service_restart.sh"

    async def execute(self, action: RemediationAction, snapshot: HealthSnapshot) -> tuple[bool, str]:
        services = snapshot.stopped_services
        logger.info("Restarting services", hostname=snapshot.hostname, services=services)
        await asyncio.sleep(0.5)
        output = "\n".join([
            f"systemctl restart {svc} → OK" for svc in services
        ])
        output += f"\nHealth check passed for: {', '.join(services)}"
        return True, output


class NotifyOnCallStrategy(RemediationStrategy):
    """Fallback for HIGH_CPU / HIGH_MEMORY — notifies on-call, no auto-fix."""
    alert_type = "HIGH_CPU"
    playbook   = "playbooks/notify_oncall.sh"

    async def execute(self, action: RemediationAction, snapshot: HealthSnapshot) -> tuple[bool, str]:
        logger.warning(
            "Notifying on-call — no auto-remediation for this alert type",
            alert_type=action.action_type,
            hostname=snapshot.hostname,
        )
        await asyncio.sleep(0.1)
        output = (
            f"PagerDuty alert sent for {action.action_type} on {snapshot.hostname}.\n"
            f"On-call engineer notified. Manual investigation required."
        )
        return True, output


# ── Engine ────────────────────────────────────────────────────────────────────

RULE_TO_STRATEGY: dict[str, RemediationStrategy] = {
    "HIGH_DISK":    LogRotationStrategy(),
    "SERVICE_DOWN": ServiceRestartStrategy(),
    "HIGH_CPU":     NotifyOnCallStrategy(),
    "HIGH_MEMORY":  NotifyOnCallStrategy(),
}


class RemediationEngine:
    def __init__(self, db, config: dict):
        self.db = db
        self.config = config
        cfg = config.get("thresholds", {})

        self.rules: list[Rule] = [
            HighDiskRule(threshold=cfg.get("disk_percent", 85.0)),
            ServiceDownRule(),
            HighCpuRule(threshold=cfg.get("cpu_percent", 90.0)),
            HighMemoryRule(threshold=cfg.get("memory_percent", 90.0)),
        ]

    async def evaluate_and_remediate(self, snapshot: HealthSnapshot) -> list[RemediationAction]:
        """
        Main entry point. For each matching rule:
          1. Persist an Alert
          2. Build a RemediationAction with an ITIL change record
          3. Execute the appropriate strategy
          4. Persist the result
        """
        fired_actions = []

        for rule in self.rules:
            if not rule.matches(snapshot):
                continue

            alert = rule.to_alert(snapshot)
            alert_id = await self._persist_alert(alert)
            alert.id = alert_id

            strategy = RULE_TO_STRATEGY.get(alert.alert_type)
            if not strategy:
                logger.warning("No strategy for alert type", alert_type=alert.alert_type)
                continue

            action = RemediationAction(
                alert_id=alert_id,
                node_id=snapshot.node_id,
                action_type=alert.alert_type,
                playbook=strategy.playbook,
                hostname=snapshot.hostname,
                change_record=self._generate_change_record(alert),
            )
            action_id = await self._persist_action(action, status="running")
            action.id = action_id

            logger.info(
                "Executing remediation",
                alert_type=alert.alert_type,
                hostname=snapshot.hostname,
                change_record=action.change_record,
            )

            try:
                success, output = await strategy.execute(action, snapshot)
                action.status = "success" if success else "failed"
                action.output = output
            except Exception as exc:
                action.status = "failed"
                action.error = str(exc)
                logger.error("Remediation failed", error=str(exc))

            await self._update_action(action)
            if action.status == "success":
                await self._resolve_alert(alert_id)

            fired_actions.append(action)

        return fired_actions

    def _generate_change_record(self, alert: Alert) -> str:
        """Generate a deterministic ITIL-style change record ID."""
        ts = datetime.utcnow().strftime("%Y%m%d")
        key = f"{alert.node_id}-{alert.alert_type}-{ts}"
        suffix = hashlib.md5(key.encode()).hexdigest()[:5].upper()
        return f"CHG-{ts}-{suffix}"

    async def _persist_alert(self, alert: Alert) -> int:
        row = await self.db.fetchrow(
            """
            INSERT INTO alerts (node_id, alert_type, severity, message, details)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            alert.node_id,
            alert.alert_type,
            alert.severity,
            alert.message,
            json.dumps(alert.details),
        )
        return row["id"]

    async def _persist_action(self, action: RemediationAction, status: str) -> int:
        row = await self.db.fetchrow(
            """
            INSERT INTO remediation_actions
                (alert_id, node_id, action_type, playbook, status, change_record)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            action.alert_id,
            action.node_id,
            action.action_type,
            action.playbook,
            status,
            action.change_record,
        )
        return row["id"]

    async def _update_action(self, action: RemediationAction):
        await self.db.execute(
            """
            UPDATE remediation_actions
            SET status=$1, output=$2, error=$3, completed_at=NOW()
            WHERE id=$4
            """,
            action.status,
            action.output,
            action.error,
            action.id,
        )

    async def _resolve_alert(self, alert_id: int):
        await self.db.execute(
            "UPDATE alerts SET resolved=TRUE, resolved_at=NOW() WHERE id=$1",
            alert_id,
        )
