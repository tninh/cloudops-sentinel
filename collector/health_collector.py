"""
collector/health_collector.py — Async node health collector.

Polls all registered nodes concurrently using asyncio + httpx.
Persists snapshots to PostgreSQL and returns results for downstream
processing by the RemediationEngine.

Design notes:
- Async so N nodes don't block each other (critical at scale)
- ConfigManager injects thresholds — no hardcoded magic numbers
- Dependency injection on db/config makes unit testing trivial
"""

import asyncio
import logging
from datetime import datetime

import httpx
import structlog

from collector.models import HealthSnapshot, Node

logger = structlog.get_logger(__name__)


class NodeHealthCollector:
    def __init__(self, db, config: dict, timeout: float = 5.0):
        """
        Args:
            db:      Database client (PostgresClient)
            config:  Loaded config dict from config/baseline.yml
            timeout: Per-node HTTP timeout in seconds
        """
        self.db = db
        self.config = config
        self.timeout = timeout

    async def collect_all(self, nodes: list[Node]) -> list[HealthSnapshot]:
        """
        Poll all nodes concurrently and return their health snapshots.
        Nodes that fail to respond are logged but don't abort the run.
        """
        logger.info("Starting collection cycle", node_count=len(nodes))

        tasks = [self._collect_node(node) for node in nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        snapshots = []
        for node, result in zip(nodes, results):
            if isinstance(result, Exception):
                logger.error(
                    "Collection failed for node",
                    hostname=node.hostname,
                    error=str(result),
                )
                await self._mark_node_unreachable(node)
            else:
                snapshots.append(result)

        logger.info(
            "Collection cycle complete",
            success=len(snapshots),
            failed=len(nodes) - len(snapshots),
        )
        return snapshots

    async def _collect_node(self, node: Node) -> HealthSnapshot:
        """Fetch metrics from a single node agent and persist."""
        url = f"http://{node.hostname}:9100/metrics"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            raw = response.json()

        snapshot = HealthSnapshot.from_raw(node.id, raw)
        await self._persist_snapshot(snapshot)
        await self._update_last_seen(node)

        logger.debug(
            "Node collected",
            hostname=node.hostname,
            cpu=snapshot.cpu_percent,
            disk=snapshot.disk_percent,
            stopped_services=snapshot.stopped_services,
        )
        return snapshot

    async def _persist_snapshot(self, snapshot: HealthSnapshot):
        await self.db.execute(
            """
            INSERT INTO health_snapshots (
                node_id, cpu_percent, memory_percent, disk_percent,
                load_avg_1m, load_avg_5m, load_avg_15m,
                running_services, stopped_services, open_ports, raw_metrics
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            snapshot.node_id,
            snapshot.cpu_percent,
            snapshot.memory_percent,
            snapshot.disk_percent,
            snapshot.load_avg_1m,
            snapshot.load_avg_5m,
            snapshot.load_avg_15m,
            snapshot.running_services,
            snapshot.stopped_services,
            snapshot.open_ports,
            snapshot.raw_metrics,
        )

    async def _update_last_seen(self, node: Node):
        await self.db.execute(
            "UPDATE nodes SET last_seen = $1 WHERE id = $2",
            datetime.utcnow(),
            node.id,
        )

    async def _mark_node_unreachable(self, node: Node):
        await self.db.execute(
            "UPDATE nodes SET last_seen = $1 WHERE id = $2",
            datetime.utcnow(),
            node.id,
        )
        logger.warning("Node unreachable", hostname=node.hostname)
