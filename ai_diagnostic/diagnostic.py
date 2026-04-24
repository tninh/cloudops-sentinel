"""
ai_diagnostic/diagnostic.py — AI-powered incident triage.

Sends alert context + recent logs to Claude and gets back a structured
diagnosis: root cause, confidence score, recommended actions, and a
human-escalation flag.

Design philosophy:
  - AI is a first-pass triage assistant, NOT an autonomous decision maker
  - escalate_to_human=True whenever confidence < 0.7 or the alert is novel
  - Every diagnosis is persisted so we can audit AI recommendations vs outcomes
  - The prompt is explicit about output format (JSON) to avoid parsing failures
"""

import json
import re
from datetime import datetime, timedelta

import anthropic
import structlog

from collector.models import Alert, Diagnosis

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are an expert Site Reliability Engineer (SRE) for a large-scale SaaS cloud platform running on Linux (CentOS/RHEL).

Your job is to analyze infrastructure alerts and recent log data, then produce a structured diagnosis.

You must respond ONLY with a valid JSON object — no markdown, no preamble, no explanation outside the JSON.

JSON schema:
{
  "root_cause": "<concise technical explanation of what is likely wrong>",
  "confidence": <float between 0.0 and 1.0>,
  "recommended_actions": ["<action 1>", "<action 2>", ...],
  "escalate_to_human": <true if confidence < 0.7 or situation is novel/ambiguous>,
  "severity_assessment": "<critical|high|medium|low>",
  "estimated_resolution_time": "<e.g. 5 minutes, 30 minutes, unknown>"
}"""


class AIOpsDiagnostic:
    def __init__(self, db, api_key: str):
        self.db = db
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = "claude-opus-4-6"

    async def diagnose(self, alert: Alert, recent_logs: list[str]) -> Diagnosis:
        """
        Build a prompt from the alert + logs, call Claude, parse the response.
        Always persists the diagnosis to the DB for audit trail.
        """
        prompt = self._build_prompt(alert, recent_logs)
        logger.info(
            "Calling AI diagnostic",
            alert_type=alert.alert_type,
            hostname=alert.hostname,
        )

        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            raw_response = message.content[0].text
            tokens_used  = message.usage.input_tokens + message.usage.output_tokens

            parsed = self._parse_response(raw_response)

            diagnosis = Diagnosis(
                alert_id=alert.id,
                node_id=alert.node_id,
                root_cause=parsed.get("root_cause", "Unable to determine root cause"),
                confidence=float(parsed.get("confidence", 0.5)),
                recommended_actions=parsed.get("recommended_actions", []),
                escalate_to_human=parsed.get("escalate_to_human", True),
                model=self.model,
                tokens_used=tokens_used,
                raw_response=raw_response,
            )

            logger.info(
                "AI diagnosis complete",
                hostname=alert.hostname,
                confidence=diagnosis.confidence,
                escalate=diagnosis.escalate_to_human,
            )

            await self._persist_diagnosis(diagnosis)
            return diagnosis

        except anthropic.APIError as e:
            logger.error("Anthropic API error", error=str(e))
            return self._fallback_diagnosis(alert)

    def _build_prompt(self, alert: Alert, logs: list[str]) -> str:
        log_block = "\n".join(logs[-20:]) if logs else "No recent logs available."

        return f"""
ALERT
-----
Type:     {alert.alert_type}
Severity: {alert.severity}
Host:     {alert.hostname}
Message:  {alert.message}
Details:  {json.dumps(alert.details, indent=2)}
Time:     {alert.triggered_at.isoformat() if alert.triggered_at else 'unknown'}

RECENT LOGS (last 20 entries from this node)
--------------------------------------------
{log_block}

Based on the alert and logs above, provide your diagnosis as JSON.
""".strip()

    def _parse_response(self, raw: str) -> dict:
        """Parse JSON from Claude response, handling minor formatting issues."""
        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?", "", raw).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Failed to parse AI response as JSON", raw=raw[:200])
            return {
                "root_cause": "AI response parsing failed — manual review required",
                "confidence": 0.0,
                "recommended_actions": ["Review logs manually", "Escalate to senior SRE"],
                "escalate_to_human": True,
            }

    def _fallback_diagnosis(self, alert: Alert) -> Diagnosis:
        """Returns a safe default when the AI call fails."""
        return Diagnosis(
            alert_id=alert.id,
            node_id=alert.node_id,
            root_cause="AI diagnostic unavailable — API error",
            confidence=0.0,
            recommended_actions=["Investigate manually", "Check on-call runbook"],
            escalate_to_human=True,
            model="fallback",
        )

    async def _persist_diagnosis(self, diagnosis: Diagnosis):
        await self.db.execute(
            """
            INSERT INTO ai_diagnoses (
                alert_id, node_id, model, root_cause, confidence,
                recommended_actions, escalate_to_human, raw_response, tokens_used
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            diagnosis.alert_id,
            diagnosis.node_id,
            diagnosis.model,
            diagnosis.root_cause,
            diagnosis.confidence,
            json.dumps(diagnosis.recommended_actions),
            diagnosis.escalate_to_human,
            diagnosis.raw_response,
            diagnosis.tokens_used,
        )

    async def get_recent_logs(self, node_id: int, window_minutes: int = 30) -> list[str]:
        """
        Fetch recent log entries for a node from stored snapshots.
        In production this would query Splunk/Logstash.
        """
        rows = await self.db.fetch(
            """
            SELECT raw_metrics->'recent_logs' AS logs
            FROM health_snapshots
            WHERE node_id = $1
              AND collected_at > NOW() - INTERVAL '$2 minutes'
            ORDER BY collected_at DESC
            LIMIT 10
            """,
            node_id,
            window_minutes,
        )

        all_logs = []
        for row in rows:
            if row["logs"]:
                entries = json.loads(row["logs"]) if isinstance(row["logs"], str) else row["logs"]
                all_logs.extend(entries or [])

        return all_logs
