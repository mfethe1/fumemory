import json
import os
from datetime import datetime

BACKLOG_PATH = "/Volumes/Edrive/Projects/agent-coordination/BACKLOG.md"
DAG_JSON_PATH = "/Volumes/Edrive/Projects/agent-coordination/BACKLOG_DAG.json"
EVENTS_LOG_PATH = "/Volumes/Edrive/Projects/agent-coordination/BACKLOG_EVENTS.jsonl"

def log_event(event_type, payload):
    event = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event_type": event_type,
        "payload": payload,
        "gateway_id": "lenny-linux"
    }
    with open(EVENTS_LOG_PATH, "a") as f:
        f.write(json.dumps(event) + "\n")

# Simple bootstrap for the migration task
if not os.path.exists(EVENTS_LOG_PATH):
    log_event("SYSTEM_BOOTSTRAP", {"message": "Initializing Distributed Agentic OS Ledger"})
    log_event("TASK_DRAFTED", {
        "id": "C7",
        "title": "Full NATS + Postgres Swarm Migration",
        "parent_id": "ROOT",
        "owner": "Mack",
        "status": "OPEN",
        "depends_on": ["C5"]
    })

print("Bootstrap event logged.")
