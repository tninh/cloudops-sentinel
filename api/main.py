"""
api/main.py — FastAPI application entry point.

Exposes REST endpoints for:
  - Node health dashboard
  - Alert management
  - Remediation history
  - Manual collection trigger
  - AI diagnostic trigger

Wires up all layers: collector → remediator → drift_detector → ai_diagnostic
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import structlog
import yaml
from fastapi import Depends, FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ai_diagnostic.diagnostic import AIOpsDiagnostic
from collector.health_collector import NodeHealthCollector
from collector.models import Alert, Node
from drift_detector.detector import DriftDetector
from remediator.engine import RemediationEngine

logger = structlog.get_logger(__name__)

# ── App state ─────────────────────────────────────────────────────────────────

app_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB pool and services on startup."""
    db_url = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql://")
    pool = await asyncpg.create_pool(dsn=os.environ["DATABASE_URL"], min_size=2, max_size=10)
    app_state["db"] = pool

    with open("config/baseline.yml") as f:
        config = yaml.safe_load(f)

    app_state["config"]         = config
    app_state["collector"]      = NodeHealthCollector(db=pool, config=config)
    app_state["remediator"]     = RemediationEngine(db=pool, config=config)
    app_state["drift_detector"] = DriftDetector(db=pool)
    app_state["ai_diagnostic"]  = AIOpsDiagnostic(
        db=pool,
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )

    logger.info("CloudOps Sentinel API started")
    yield

    await pool.close()
    logger.info("CloudOps Sentinel API stopped")


app = FastAPI(
    title="CloudOps Sentinel",
    description="Infrastructure Health & Remediation Platform",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Dependency helpers ────────────────────────────────────────────────────────

def get_db():
    return app_state["db"]

def get_collector():
    return app_state["collector"]

def get_remediator():
    return app_state["remediator"]

def get_drift():
    return app_state["drift_detector"]

def get_ai():
    return app_state["ai_diagnostic"]


# ── Pydantic response models ──────────────────────────────────────────────────

class NodeResponse(BaseModel):
    id: int
    hostname: str
    role: str
    environment: str
    active: bool
    last_seen: Optional[str]

class SnapshotResponse(BaseModel):
    hostname: str
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    load_avg_1m: float
    running_services: list[str]
    stopped_services: list[str]
    is_healthy: bool
    collected_at: str

class AlertResponse(BaseModel):
    id: int
    hostname: str
    alert_type: str
    severity: str
    message: str
    resolved: bool
    triggered_at: str

class CollectionResult(BaseModel):
    nodes_polled: int
    alerts_fired: int
    remediations_executed: int
    drift_reports: int


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service": "CloudOps Sentinel",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.get("/nodes", response_model=list[NodeResponse])
async def list_nodes(db=Depends(get_db)):
    """List all registered nodes."""
    rows = await db.fetch(
        "SELECT id, hostname, role, environment, active, last_seen FROM nodes ORDER BY hostname"
    )
    return [
        NodeResponse(
            id=r["id"],
            hostname=r["hostname"],
            role=r["role"],
            environment=r["environment"],
            active=r["active"],
            last_seen=r["last_seen"].isoformat() if r["last_seen"] else None,
        )
        for r in rows
    ]


@app.get("/nodes/{hostname}/health")
async def node_health(hostname: str, db=Depends(get_db)):
    """Get the latest health snapshot for a node."""
    row = await db.fetchrow(
        """
        SELECT hs.*, n.hostname, n.role
        FROM health_snapshots hs
        JOIN nodes n ON n.id = hs.node_id
        WHERE n.hostname = $1
        ORDER BY hs.collected_at DESC
        LIMIT 1
        """,
        hostname,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"No health data for {hostname}")

    return {
        "hostname":          row["hostname"],
        "role":              row["role"],
        "cpu_percent":       float(row["cpu_percent"] or 0),
        "memory_percent":    float(row["memory_percent"] or 0),
        "disk_percent":      float(row["disk_percent"] or 0),
        "load_avg_1m":       float(row["load_avg_1m"] or 0),
        "running_services":  row["running_services"] or [],
        "stopped_services":  row["stopped_services"] or [],
        "collected_at":      row["collected_at"].isoformat(),
    }


@app.get("/alerts", response_model=list[AlertResponse])
async def list_alerts(resolved: bool = False, db=Depends(get_db)):
    """List alerts, defaulting to unresolved only."""
    rows = await db.fetch(
        """
        SELECT a.id, n.hostname, a.alert_type, a.severity,
               a.message, a.resolved, a.triggered_at
        FROM alerts a
        JOIN nodes n ON n.id = a.node_id
        WHERE a.resolved = $1
        ORDER BY a.triggered_at DESC
        LIMIT 100
        """,
        resolved,
    )
    return [
        AlertResponse(
            id=r["id"],
            hostname=r["hostname"],
            alert_type=r["alert_type"],
            severity=r["severity"],
            message=r["message"],
            resolved=r["resolved"],
            triggered_at=r["triggered_at"].isoformat(),
        )
        for r in rows
    ]


@app.get("/remediations")
async def list_remediations(db=Depends(get_db)):
    """List recent remediation actions with ITIL change records."""
    rows = await db.fetch(
        """
        SELECT ra.id, n.hostname, ra.action_type, ra.playbook,
               ra.status, ra.change_record, ra.started_at, ra.completed_at, ra.output
        FROM remediation_actions ra
        JOIN nodes n ON n.id = ra.node_id
        ORDER BY ra.started_at DESC
        LIMIT 50
        """
    )
    return [dict(r) for r in rows]


@app.post("/collect", response_model=CollectionResult)
async def trigger_collection(
    db=Depends(get_db),
    collector=Depends(get_collector),
    remediator=Depends(get_remediator),
    drift=Depends(get_drift),
):
    """
    Trigger a full collection cycle:
    1. Poll all active nodes
    2. Evaluate rules and execute remediations
    3. Check for config drift
    """
    nodes_rows = await db.fetch(
        "SELECT id, hostname, role, environment, active FROM nodes WHERE active = TRUE"
    )
    nodes = [
        Node(
            id=r["id"],
            hostname=r["hostname"],
            role=r["role"],
            environment=r["environment"],
            active=r["active"],
        )
        for r in nodes_rows
    ]

    snapshots   = await collector.collect_all(nodes)
    all_actions = []
    drift_count = 0

    # Build hostname→role map for drift detection
    role_map = {r["hostname"]: r["role"] for r in nodes_rows}

    for snapshot in snapshots:
        actions = await remediator.evaluate_and_remediate(snapshot)
        all_actions.extend(actions)

        node_role = role_map.get(snapshot.hostname, "unknown")
        report = drift.detect(snapshot, node_role)
        if report:
            await drift.persist_report(report)
            drift_count += 1

    alerts_fired = len(all_actions)

    return CollectionResult(
        nodes_polled=len(snapshots),
        alerts_fired=alerts_fired,
        remediations_executed=len([a for a in all_actions if a.status == "success"]),
        drift_reports=drift_count,
    )


@app.post("/diagnose/{alert_id}")
async def diagnose_alert(
    alert_id: int,
    db=Depends(get_db),
    ai=Depends(get_ai),
):
    """Run AI diagnostic on an existing alert."""
    row = await db.fetchrow(
        """
        SELECT a.*, n.hostname
        FROM alerts a JOIN nodes n ON n.id = a.node_id
        WHERE a.id = $1
        """,
        alert_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

    alert = Alert(
        id=row["id"],
        node_id=row["node_id"],
        hostname=row["hostname"],
        alert_type=row["alert_type"],
        severity=row["severity"],
        message=row["message"],
        details=dict(row["details"]) if row["details"] else {},
        triggered_at=row["triggered_at"],
    )

    recent_logs = await ai.get_recent_logs(alert.node_id)
    diagnosis   = await ai.diagnose(alert, recent_logs)

    return {
        "alert_id":           alert_id,
        "hostname":           alert.hostname,
        "root_cause":         diagnosis.root_cause,
        "confidence":         diagnosis.confidence,
        "recommended_actions": diagnosis.recommended_actions,
        "escalate_to_human":  diagnosis.escalate_to_human,
        "model":              diagnosis.model,
        "tokens_used":        diagnosis.tokens_used,
    }


@app.get("/dashboard")
async def dashboard(db=Depends(get_db)):
    """Aggregated dashboard data for the UI."""
    nodes_total  = await db.fetchval("SELECT COUNT(*) FROM nodes WHERE active = TRUE")
    alerts_open  = await db.fetchval("SELECT COUNT(*) FROM alerts WHERE resolved = FALSE")
    remediations = await db.fetchval("SELECT COUNT(*) FROM remediation_actions WHERE status = 'success'")
    drift_count  = await db.fetchval("SELECT COUNT(*) FROM drift_reports WHERE remediated = FALSE")

    recent_alerts = await db.fetch(
        """
        SELECT a.alert_type, a.severity, n.hostname, a.triggered_at
        FROM alerts a JOIN nodes n ON n.id = a.node_id
        WHERE a.resolved = FALSE
        ORDER BY a.triggered_at DESC LIMIT 5
        """
    )

    return {
        "summary": {
            "nodes_monitored":       nodes_total,
            "open_alerts":           alerts_open,
            "remediations_today":    remediations,
            "drift_violations":      drift_count,
        },
        "recent_alerts": [dict(r) for r in recent_alerts],
    }
