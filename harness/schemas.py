from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8192)]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Objective(Schema):
    objective: Text


class UserMessage(Schema):
    message: Text


class Decision(Schema):
    decision: Literal["approve", "reject"]


class Query(Schema):
    query: Text


class Service(Schema):
    service_name: Text


class Status(Service):
    status: Literal["healthy", "degraded", "down"]
    duration_minutes: int = Field(ge=0)


class Article(Schema):
    doc_id: Text
    title: Text
    body: Text


class Matches(Schema):
    matches: list[Article]


class Incident(Schema):
    title: Text
    description: Text
    severity: Literal["P1", "P2", "P3", "P4"]


class Receipt(Schema):
    incident_id: Text


class ToolCall(Schema):
    id: Text
    name: Text
    arguments: dict


class Answer(Schema):
    text: Text
