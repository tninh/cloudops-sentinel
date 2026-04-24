#!/usr/bin/env python3
"""
node_agent.py — Simulated Linux node metrics agent.

Runs inside each Docker container and exposes a /metrics endpoint.
Simulates realistic CPU/memory/disk/service data, with optional
fault injection via the SIMULATE_FAULT env var.

Faults:  none | high_disk | high_cpu | service_down | high_memory
"""

import json
import os
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

NODE_ROLE = os.environ.get("NODE_ROLE", "web")
FAULT = os.environ.get("SIMULATE_FAULT", "none")
PORT = 9100

# Role-based service definitions
ROLE_SERVICES = {
    "web":   ["nginx", "node_exporter", "filebeat"],
    "app":   ["app_server", "gunicorn", "node_exporter", "filebeat"],
    "db":    ["postgresql", "pgbouncer", "node_exporter", "filebeat"],
    "cache": ["redis", "node_exporter", "filebeat"],
}

def build_metrics() -> dict:
    services = ROLE_SERVICES.get(NODE_ROLE, ["node_exporter"])

    # Base healthy metrics with small random noise
    cpu     = round(random.uniform(15, 45), 2)
    memory  = round(random.uniform(40, 65), 2)
    disk    = round(random.uniform(30, 55), 2)
    load_1  = round(random.uniform(0.5, 2.0), 3)
    load_5  = round(random.uniform(0.4, 1.8), 3)
    load_15 = round(random.uniform(0.3, 1.5), 3)

    running  = list(services)
    stopped  = []
    ports    = [22, 9100]
    logs     = []

    # ── Fault injection ───────────────────────────────────────────
    if FAULT == "high_disk":
        disk = round(random.uniform(88, 97), 2)
        logs = [
            "ERROR kernel: EXT4-fs error: no space left on device",
            "ERROR nginx: could not write to error log /var/log/nginx/error.log",
            f"WARNING disk usage at {disk}% on /dev/sda1",
        ]

    elif FAULT == "high_cpu":
        cpu    = round(random.uniform(91, 99), 2)
        load_1 = round(random.uniform(8.0, 16.0), 3)
        logs   = [
            "WARNING high CPU utilization detected",
            "ERROR process app_server consumed >90% CPU for 120s",
        ]

    elif FAULT == "service_down":
        # Pull the primary service for this role off the running list
        primary = services[0] if services else "unknown_service"
        running = [s for s in services if s != primary]
        stopped = [primary]
        logs    = [
            f"CRITICAL {primary} process exited unexpectedly (exit code 1)",
            f"ERROR systemd: {primary}.service failed",
            f"WARNING health check failed for {primary} — 3 consecutive failures",
        ]
        ports   = [22, 9100]  # primary service port gone

    elif FAULT == "high_memory":
        memory = round(random.uniform(90, 98), 2)
        logs   = [
            "ERROR Out of Memory: Kill process (OOMKiller triggered)",
            f"WARNING memory usage at {memory}%",
        ]

    return {
        "hostname":     os.environ.get("HOSTNAME", "unknown"),
        "role":         NODE_ROLE,
        "fault":        FAULT,
        "timestamp":    time.time(),
        "cpu_percent":  cpu,
        "memory_percent": memory,
        "disk_percent": disk,
        "load_avg": {
            "1m":  load_1,
            "5m":  load_5,
            "15m": load_15,
        },
        "running_services": running,
        "stopped_services": stopped,
        "open_ports":   ports,
        "recent_logs":  logs,
        "os": {
            "distro":  "CentOS Linux 8",
            "kernel":  "4.18.0-348.el8.x86_64",
            "uptime_hours": round(random.uniform(100, 5000), 1),
        },
        "packages": {
            "nginx":      "1.20.1",
            "postgresql": "13.4",
            "redis":      "6.2.6",
            "python3":    "3.9.7",
        },
    }


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            payload = json.dumps(build_metrics(), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(payload))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress default HTTP logs


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), MetricsHandler)
    print(f"[node-agent] {os.environ.get('HOSTNAME')} ({NODE_ROLE}) "
          f"fault={FAULT} listening on :{PORT}")
    server.serve_forever()
