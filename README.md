# CloudOps Sentinel

**Infrastructure Health & Automated Remediation Platform**

A production-grade SRE tooling project that demonstrates the skills required for a Cloud Operations / Systems Administration role with a strong development focus. Built in Python, Bash, and SQL across five distinct layers.

```
                    ┌─────────────────────────────────────────┐
                    │         CloudOps Sentinel                │
                    │                                         │
  ┌──────────┐      │  ┌───────────┐    ┌─────────────────┐  │
  │ node-01  │─────▶│  │  Health   │───▶│  Remediation    │  │
  │ node-02  │      │  │ Collector │    │    Engine       │  │
  │ node-03* │      │  │ (async)   │    │ (Strategy pat.) │  │
  │ node-04* │      │  └───────────┘    └────────┬────────┘  │
  │ node-05  │      │        │                   │           │
  └──────────┘      │        ▼                   ▼           │
  * fault injected  │  ┌──────────┐    ┌─────────────────┐  │
                    │  │  Drift   │    │  AI Diagnostic  │  │
                    │  │ Detector │    │  (Claude API)   │  │
                    │  └──────────┘    └─────────────────┘  │
                    │        │                   │           │
                    │        ▼                   ▼           │
                    │  ┌─────────────────────────────────┐   │
                    │  │     PostgreSQL  (audit trail)   │   │
                    │  └─────────────────────────────────┘   │
                    │              │                          │
                    │        ┌─────▼──────┐                  │
                    │        │  FastAPI   │                  │
                    │        │    REST    │                  │
                    │        └────────────┘                  │
                    └─────────────────────────────────────────┘
```

---

## What This Project Demonstrates

| Job Requirement | Implementation |
|---|---|
| Python development for infrastructure tooling | `collector/`, `remediator/`, `drift_detector/`, `ai_diagnostic/` |
| Bash scripting for automation | `patching/patch_orchestrator.sh` |
| Configuration management (Puppet/Ansible analog) | `drift_detector/` + `config/baseline.yml` |
| Large-scale patching automation | Canary + batch + rollback orchestrator |
| AI integration in operational workflows | Claude-powered incident triage |
| PostgreSQL / database proficiency | Full schema, async queries via asyncpg |
| ITIL change management | Every auto-remediation generates a `CHG-YYYYMMDD-XXXXX` record |
| Design patterns & software fundamentals | Strategy pattern, dependency injection, dataclasses |
| Production monitoring concepts | Alert thresholds, health snapshots, drift scoring |

---

## Project Structure

```
cloudops-sentinel/
├── collector/
│   ├── models.py          # Domain dataclasses (Node, HealthSnapshot, Alert, ...)
│   └── health_collector.py # Async concurrent node poller
├── remediator/
│   └── engine.py          # Rule evaluation + Strategy-pattern remediation
├── drift_detector/
│   └── detector.py        # Puppet-style config baseline enforcement
├── ai_diagnostic/
│   └── diagnostic.py      # Claude-powered incident triage
├── api/
│   └── main.py            # FastAPI REST layer
├── patching/
│   └── patch_orchestrator.sh  # Canary + batched + rollback patching
├── scripts/
│   └── node_agent.py      # Simulated Linux node metrics agent
├── db/
│   └── schema.sql         # Full PostgreSQL schema
├── config/
│   └── baseline.yml       # Desired state declarations per role
├── tests/
│   └── test_sentinel.py   # Pytest suite (10 tests)
├── docker-compose.yml     # 5 nodes + postgres + api
├── Dockerfile
└── requirements.txt
```

---

## Quick Start

### Prerequisites
- Docker Desktop
- Python 3.11+
- An Anthropic API key (for the AI diagnostic layer)

### 1. Clone and configure

```bash
git clone https://github.com/yourname/cloudops-sentinel
cd cloudops-sentinel
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY
```

### 2. Start the platform

```bash
docker-compose up -d
```

This starts:
- `sentinel-db` — PostgreSQL with schema auto-applied
- `node-01` through `node-05` — simulated Linux nodes (node-03 has `high_disk` fault, node-04 has `service_down` fault)
- `sentinel-api` — FastAPI on http://localhost:8000

### 3. Trigger a collection cycle

```bash
curl -X POST http://localhost:8000/collect | python3 -m json.tool
```

Expected output:
```json
{
  "nodes_polled": 5,
  "alerts_fired": 2,
  "remediations_executed": 2,
  "drift_reports": 1
}
```

### 4. View the dashboard

```bash
curl http://localhost:8000/dashboard | python3 -m json.tool
```

### 5. See active alerts

```bash
curl http://localhost:8000/alerts | python3 -m json.tool
```

### 6. Run AI diagnostic on an alert

```bash
# Replace 1 with an actual alert ID from the previous step
curl -X POST http://localhost:8000/diagnose/1 | python3 -m json.tool
```

Example AI response:
```json
{
  "root_cause": "Disk exhaustion caused by unrotated nginx access logs accumulating in /var/log/nginx/. The EXT4 filesystem has no remaining inodes for new writes.",
  "confidence": 0.91,
  "recommended_actions": [
    "Run logrotate -f /etc/logrotate.d/nginx",
    "Verify /var/log/nginx disk usage: du -sh /var/log/nginx/",
    "Review log retention policy in /etc/logrotate.conf"
  ],
  "escalate_to_human": false,
  "model": "claude-opus-4-6"
}
```

### 7. View ITIL change records

```bash
curl http://localhost:8000/remediations | python3 -m json.tool
```

### 8. Run the patch orchestrator (dry run)

```bash
cd patching
PATCH_DRY_RUN=true bash patch_orchestrator.sh
```

### 9. Run tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

### 10. Interactive API docs

Open http://localhost:8000/docs for the full Swagger UI.

---

## Architecture Deep Dive

### Async Collector
`NodeHealthCollector.collect_all()` uses `asyncio.gather()` to poll all nodes concurrently. At 100 nodes with a 5s timeout, sequential polling would take 500s; async brings that to ~5s. Failed nodes are caught individually and don't abort the collection run.

### Strategy Pattern (Remediation Engine)
Each alert type maps to a `RemediationStrategy` subclass. Adding a new remediation (e.g., `KafkaRestartStrategy`) means adding one class — the engine never changes. This satisfies the Open/Closed Principle and maps directly to how you'd extend production automation without breaking existing playbooks.

### ITIL Change Records
Every automated remediation generates a deterministic change record ID (`CHG-YYYYMMDD-XXXXX`) persisted to PostgreSQL. In a real environment this would integrate with ServiceNow's ITSM. The audit trail exists even when a bot executes the fix.

### AI as Triage, Not Autonomous Actor
The AI diagnostic layer includes an explicit `escalate_to_human: bool` field. When confidence < 0.7 or the situation is novel, the AI flags for human review rather than recommending action. This reflects production reality: LLMs accelerate triage but don't replace engineer judgment for ambiguous incidents.

### Config Drift Detection
`DriftDetector` compares live node state against YAML baselines — the same concept Puppet manifests encode, but transparent in Python. The `drift_score` property counts individual violations, making it easy to set alerting thresholds (e.g., "page if drift_score > 5").

---

## Interview Talking Points

**"Walk me through a tool you built to replace manual work."**
> The patching orchestrator. Before: an engineer SSH'd into nodes one by one. This implements canary validation, batch processing, automatic rollback when failure rate exceeds threshold, and state persistence so a mid-run restart doesn't lose progress. The structured JSON logs integrate directly with Splunk.

**"How do you approach automation at scale?"**
> The async collector is the answer — concurrent polling means adding 100 more nodes doesn't increase wall-clock collection time. I also designed the remediator so new playbook types are additive (Strategy pattern), not edits to existing code. That's critical when multiple team members are contributing automation.

**"How are you integrating AI into operational workflows?"**
> The AI diagnostic layer calls Claude with alert context and recent logs, then returns structured output including a confidence score and explicit escalation flag. The key design decision was making `escalate_to_human` a first-class field — the system knows its own limits. Every AI recommendation is persisted for audit, so we can retrospectively evaluate AI accuracy against actual outcomes.

**"Tell me about your ITIL experience."**
> Every automated remediation in this system generates a change record before execution. Alerts trigger incident records. Repeated alerts on the same node would correlate to a problem record. I modeled this explicitly because in a SaaS environment, even automated actions need an audit trail for compliance and post-incident review.

**"What's your experience with config management tools like Puppet?"**
> I built a lightweight drift detector that mirrors Puppet's core concept — declared desired state in YAML, continuous comparison against live state, violation scoring. In production I'd use Puppet for the actual enforcement, but owning the detection logic in Python means I can pipe violations into our own alerting and ticketing pipeline without depending on Puppet's reporting.
