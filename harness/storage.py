import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .protocol import display_reply


logger = logging.getLogger("uvicorn.error")
DEFAULT_STEPS = 12
DDL = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, objective TEXT NOT NULL, status TEXT NOT NULL,
    steps_used INTEGER NOT NULL DEFAULT 0, max_steps INTEGER NOT NULL,
    result TEXT, stop_reason TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
    at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_run ON run_events(run_id, seq);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
    call_id TEXT NOT NULL, args TEXT NOT NULL, decision TEXT, response TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS pending_per_run ON approvals(run_id) WHERE decision IS NULL;
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY, action_id TEXT UNIQUE NOT NULL REFERENCES approvals(id),
    title TEXT NOT NULL, description TEXT NOT NULL, severity TEXT NOT NULL
);
"""


def public_result(run):
    """Render operational facts from this snapshot, including previously cached approvals."""
    if "model_summary" not in run:
        run["model_summary"] = run["result"] if run["stop_reason"] == "answered" else None
    run.setdefault("conversation_id", run["run_id"])
    run["reply"] = display_reply(run["model_summary"])
    run["observations"] = [{"name": e["name"], "output": e["output"]} for e in run["events"]
                           if e["kind"] == "tool_result"]
    if run["status"] in {"running", "waiting_approval"}:
        run["result"] = None
        return run

    facts = []
    for event in run["events"]:
        if event["kind"] != "tool_result":
            continue
        output = event["output"]
        if event["name"] == "get_service_status":
            facts.append(f"{output['service_name']} was {output['status']} for {output['duration_minutes']} minutes.")
        elif event["name"] == "search_knowledge_base":
            matches = ", ".join(doc["doc_id"] for doc in output["matches"])
            facts.append(f"Runbook matches: {matches or 'none'}.")
    if run["stop_reason"] == "approval_rejected":
        facts.append("Incident proposal declined.")
    ids = run["incident_ids"]
    facts.append("Created incidents: " + ", ".join(ids) + "." if ids else "No local incident recorded for this run.")
    run["result"] = " ".join(facts)
    return run


class ConversationBusy(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(DDL)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            if "max_steps" not in columns:
                db.execute(f"ALTER TABLE runs ADD COLUMN max_steps INTEGER NOT NULL DEFAULT {DEFAULT_STEPS}")
            if "conversation_id" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN conversation_id TEXT")
                db.execute("UPDATE runs SET conversation_id=id")
            db.execute("CREATE INDEX IF NOT EXISTS conversation_runs ON runs(conversation_id)")
            db.commit()
        finally:
            db.close()

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, objective, max_steps, conversation=None):
        run = uuid4().hex
        with self.connection(True) as db:
            if conversation is not None:
                last = db.execute("SELECT status FROM runs WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1",
                                  (conversation,)).fetchone()
                if last is None:
                    raise KeyError(conversation)
                if last["status"] in {"running", "waiting_approval"}:
                    raise ConversationBusy(last["status"])
            db.execute("INSERT INTO runs(id,conversation_id,objective,status,max_steps) VALUES(?,?,?,'running',?)",
                       (run, conversation or run, objective, max_steps))
            self.event(db, run, "started")
        return run

    @staticmethod
    def event(db, run, kind, **payload):
        at = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO run_events(run_id,at,kind,payload) VALUES(?,?,?,?)",
                   (run, at, kind, json.dumps(payload)))
        labels = {k: v for k, v in payload.items()
                  if k in {"name", "attempt", "elapsed_ms", "error", "http_status", "status", "reason", "call_id"}}
        logger.info("%s", json.dumps({"run_id": run, "event": kind, **labels}))

    def record(self, run, kind, **payload):
        with self.connection(True) as db:
            self.event(db, run, kind, **payload)

    def finish(self, db, run, status, reason, result=None):
        db.execute("UPDATE runs SET status=?,stop_reason=?,result=? WHERE id=?",
                   (status, reason, result, run))
        self.event(db, run, "finished", status=status, reason=reason)

    def spend(self, db, run):
        changed = db.execute("UPDATE runs SET steps_used=steps_used+1 "
                             "WHERE id=? AND status='running' AND steps_used<max_steps", (run,)).rowcount
        if not changed and db.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0] == "running":
            self.finish(db, run, "limit_reached", "step_limit")
        return bool(changed)

    @staticmethod
    def snapshot(db, run):
        row = db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        if row is None:
            raise KeyError(run)
        info = dict(row)
        info["run_id"] = info.pop("id")
        info["events"] = [
            {"seq": e["seq"], "at": e["at"], "kind": e["kind"], **json.loads(e["payload"])}
            for e in db.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY seq", (run,))
        ]
        pending = db.execute("SELECT * FROM approvals WHERE run_id=? AND decision IS NULL", (run,)).fetchone()
        info["pending_action"] = ({"action_id": pending["id"], "arguments": json.loads(pending["args"])}
                                  if pending else None)
        info["incident_ids"] = [r[0] for r in db.execute(
            "SELECT i.id FROM incidents i JOIN approvals a ON a.id=i.action_id WHERE a.run_id=? ORDER BY i.rowid", (run,))]
        return public_result(info)

    def get(self, run):
        with self.connection() as db:
            return self.snapshot(db, run)

    def turns(self, conversation, before=None):
        with self.connection() as db:
            if before is None:
                rows = db.execute("SELECT id FROM runs WHERE conversation_id=? ORDER BY rowid", (conversation,))
            else:
                rows = db.execute("SELECT id FROM (SELECT rowid AS n,id FROM runs WHERE conversation_id=? "
                                  "AND rowid < (SELECT rowid FROM runs WHERE id=?) ORDER BY rowid DESC LIMIT 12) "
                                  "ORDER BY n", (conversation, before))
            turns = []
            for row in rows:
                run = self.snapshot(db, row["id"])
                turns.append({k: run[k] for k in ("run_id", "objective", "reply", "result", "status",
                                                  "pending_action", "stop_reason", "incident_ids", "observations")})
            if before is None and not turns:
                raise KeyError(conversation)
            return turns

    def recover(self):
        with self.connection(True) as db:
            for row in db.execute("SELECT id FROM runs WHERE status='running'").fetchall():
                self.finish(db, row["id"], "failed", "process_interrupted")
