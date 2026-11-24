import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .schemas import Article, Incident, Matches, Query, Receipt, Schema, Service, Status


DATA = Path(__file__).resolve().parents[1] / "data"


@dataclass(frozen=True)
class ToolSpec:
    inputs: type[Schema]
    outputs: type[Schema]
    access: Literal["read", "approval_required"]
    description: str


SPECS = {
    "search_knowledge_base": ToolSpec(Query, Matches, "read", "Search the internal operations runbooks by keywords."),
    "get_service_status": ToolSpec(Service, Status, "read", "Read the service snapshot and duration in minutes."),
    "create_incident": ToolSpec(Incident, Receipt, "approval_required", "Propose an incident for human approval."),
}


class UnknownService(Exception):
    pass


class MockTools:
    def __init__(self, folder=DATA):
        services = json.loads((folder / "services.json").read_text(encoding="utf-8"))
        self.services = {s["service_name"]: Status.model_validate(s) for s in services}
        articles = json.loads((folder / "knowledge_base.json").read_text(encoding="utf-8"))
        self.articles = [Article.model_validate(a) for a in articles]

    async def read(self, name, args):
        if name == "get_service_status":
            status = self.services.get(args.service_name.lower())
            if status is None:
                raise UnknownService(args.service_name)
            return status.model_dump()
        if name != "search_knowledge_base":
            raise ValueError("unknown read tool")
        words = set(re.findall(r"[\w-]+", args.query.lower()))
        matches = []
        for article in self.articles:
            terms = set(re.findall(r"[\w-]+", (article.title + " " + article.body).lower()))
            if words & terms:
                matches.append(article.model_dump())
        return {"matches": matches}

    async def create_incident(self, args: Incident, action_id: str, db: sqlite3.Connection):
        incident_id = "INC-" + uuid4().hex
        db.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?)",
                   (incident_id, action_id, args.title, args.description, args.severity))
        return {"incident_id": incident_id}
