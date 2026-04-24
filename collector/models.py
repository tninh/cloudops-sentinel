"""
collector/models.py — Domain models for CloudOps Sentinel.

Dataclasses represent the core entities flowing through the system.
Keeping models in a shared location avoids circular imports and makes
the domain clear to anyone reading the codebase.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Node:
    id: int
    hostname: str
    role: str           # web | app | db | cache
    environment: str
    ip_address: Optional[str] = None
    active: bool = True
    last_seen: Optional[datetime] = None


@dataclass
class HealthSnapshot:
    """
    Immutable snapshot of a node's health at a point in time.
    Created by NodeHealthCollector, persisted to health_snapshots table.
    """
    node_id: int
    hostname: str
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    load_avg_1m: float
    load_avg_5m: float
    load_avg_15m: float
    running_services: list[str] = field(default_factory=list)
    stopped_services: list[str] = field(default_factory=list)
    open_ports: list[int] = field(default_factory=list)
    recent_logs: list[str] = field(default_factory=list)
    raw_metrics: dict = field(default_factory=dict)
    collected_at: datetime = field(default_factory=datetime.utcnow)

    @classmethod
    def from_raw(cls, node_id: int, data: dict) -> "HealthSnapshot":
        load = data.get("load_avg", {})
        return cls(
            node_id=node_id,
            hostname=data.get("hostname", "unknown"),
            cpu_percent=data.get("cpu_percent", 0.0),
            memory_percent=data.get("memory_percent", 0.0),
            disk_percent=data.get("disk_percent", 0.0),
            load_avg_1m=load.get("1m", 0.0),
            load_avg_5m=load.get("5m", 0.0),
            load_avg_15m=load.get("15m", 0.0),
            running_services=data.get("running_services", []),
            stopped_services=data.get("stopped_services", []),
            open_ports=data.get("open_ports", []),
            recent_logs=data.get("recent_logs", []),
            raw_metrics=data,
        )

    @property
    def is_healthy(self) -> bool:
        return (
            self.cpu_percent < 85
            and self.memory_percent < 85
            and self.disk_percent < 85
            and not self.stopped_services
        )


@dataclass
class Alert:
    node_id: int
    hostname: str
    alert_type: str      # HIGH_DISK | HIGH_CPU | SERVICE_DOWN | HIGH_MEMORY
    severity: str        # critical | warning | info
    message: str
    details: dict = field(default_factory=dict)
    id: Optional[int] = None
    triggered_at: datetime = field(default_factory=datetime.utcnow)
    resolved: bool = False


@dataclass
class RemediationAction:
    alert_id: int
    node_id: int
    action_type: str     # LOG_ROTATION | SERVICE_RESTART | NOTIFY_ONCALL
    playbook: str
    hostname: str = ""
    id: Optional[int] = None
    status: str = "pending"
    change_record: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None


@dataclass
class DriftReport:
    node_id: int
    hostname: str
    baseline_name: str
    missing_packages: list[str] = field(default_factory=list)
    stopped_services: list[str] = field(default_factory=list)
    sysctl_drift: dict = field(default_factory=dict)
    package_version_drift: dict = field(default_factory=dict)

    @property
    def drift_score(self) -> int:
        return (
            len(self.missing_packages)
            + len(self.stopped_services)
            + len(self.sysctl_drift)
            + len(self.package_version_drift)
        )

    @property
    def has_drift(self) -> bool:
        return self.drift_score > 0


@dataclass
class Diagnosis:
    alert_id: int
    node_id: int
    root_cause: str
    confidence: float          # 0.0 – 1.0
    recommended_actions: list[str] = field(default_factory=list)
    escalate_to_human: bool = False
    model: str = ""
    tokens_used: int = 0
    raw_response: str = ""
