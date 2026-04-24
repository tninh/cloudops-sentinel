"""
drift_detector/detector.py — Configuration drift detection.

Compares live node state (from HealthSnapshot) against a declared
YAML baseline. Conceptually mirrors what Puppet/Ansible enforce,
but lightweight and inspectable in code.

Interview note: "I built a lightweight version so I could explain the
concept clearly — in production we'd use Puppet, but owning the logic
in Python means we can integrate it into our own alerting pipeline."
"""

import json
from dataclasses import dataclass, field
from typing import Optional

import structlog
import yaml

from collector.models import DriftReport, HealthSnapshot

logger = structlog.get_logger(__name__)


@dataclass
class BaselineConfig:
    """
    Declared desired state for a node role.
    Loaded from config/baseline.yml.
    """
    name: str
    roles: list[str]                         # which node roles this applies to
    required_packages: list[str] = field(default_factory=list)
    required_services: list[str] = field(default_factory=list)
    package_versions: dict[str, str] = field(default_factory=dict)
    sysctl_params: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "BaselineConfig":
        return cls(
            name=name,
            roles=data.get("roles", []),
            required_packages=data.get("required_packages", []),
            required_services=data.get("required_services", []),
            package_versions=data.get("package_versions", {}),
            sysctl_params=data.get("sysctl_params", {}),
        )


class DriftDetector:
    def __init__(self, db, baseline_path: str = "config/baseline.yml"):
        self.db = db
        self.baselines = self._load_baselines(baseline_path)

    def _load_baselines(self, path: str) -> list[BaselineConfig]:
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            baselines = []
            for name, cfg in data.get("baselines", {}).items():
                baselines.append(BaselineConfig.from_dict(name, cfg))
            logger.info("Baselines loaded", count=len(baselines))
            return baselines
        except FileNotFoundError:
            logger.warning("Baseline config not found, using defaults", path=path)
            return [self._default_baseline()]

    def _default_baseline(self) -> BaselineConfig:
        return BaselineConfig(
            name="default",
            roles=["web", "app", "db", "cache"],
            required_packages=["nginx", "python3", "curl"],
            required_services=["node_exporter", "filebeat"],
            sysctl_params={"vm.swappiness": "10"},
        )

    def detect(self, snapshot: HealthSnapshot, node_role: str) -> Optional[DriftReport]:
        """
        Compare the snapshot against all applicable baselines.
        Returns a DriftReport if drift is found, else None.
        """
        applicable = [b for b in self.baselines if node_role in b.roles]
        if not applicable:
            logger.debug("No baseline for role", role=node_role)
            return None

        # Merge all applicable baselines (role-specific overrides generic)
        baseline = applicable[-1]

        live_packages  = set(snapshot.raw_metrics.get("packages", {}).keys())
        live_services  = set(snapshot.running_services)
        live_pkg_vers  = snapshot.raw_metrics.get("packages", {})

        missing_packages = [
            p for p in baseline.required_packages
            if p not in live_packages
        ]

        stopped_required = [
            s for s in baseline.required_services
            if s in snapshot.stopped_services
        ]

        # Package version drift
        version_drift = {}
        for pkg, expected_ver in baseline.package_versions.items():
            live_ver = live_pkg_vers.get(pkg)
            if live_ver and live_ver != expected_ver:
                version_drift[pkg] = {
                    "expected": expected_ver,
                    "actual": live_ver,
                }

        # Sysctl drift — in production we'd SSH to node;
        # here we simulate based on known node state
        sysctl_drift = {}
        live_sysctl = snapshot.raw_metrics.get("sysctl", {})
        for param, expected_val in baseline.sysctl_params.items():
            actual_val = live_sysctl.get(param)
            if actual_val is not None and actual_val != expected_val:
                sysctl_drift[param] = {
                    "expected": expected_val,
                    "actual": actual_val,
                }

        report = DriftReport(
            node_id=snapshot.node_id,
            hostname=snapshot.hostname,
            baseline_name=baseline.name,
            missing_packages=missing_packages,
            stopped_services=stopped_required,
            sysctl_drift=sysctl_drift,
            package_version_drift=version_drift,
        )

        if report.has_drift:
            logger.warning(
                "Config drift detected",
                hostname=snapshot.hostname,
                drift_score=report.drift_score,
                missing_packages=missing_packages,
                stopped_services=stopped_required,
            )
        else:
            logger.debug("No drift detected", hostname=snapshot.hostname)

        return report if report.has_drift else None

    async def persist_report(self, report: DriftReport):
        await self.db.execute(
            """
            INSERT INTO drift_reports (
                node_id, baseline_name, missing_packages,
                stopped_services, sysctl_drift, package_version_drift, drift_score
            ) VALUES ($1,$2,$3,$4,$5,$6,$7)
            """,
            report.node_id,
            report.baseline_name,
            json.dumps(report.missing_packages),
            json.dumps(report.stopped_services),
            json.dumps(report.sysctl_drift),
            json.dumps(report.package_version_drift),
            report.drift_score,
        )
