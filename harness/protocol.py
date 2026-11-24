"""Model-facing view of a run, shared by the demo and live adapters."""

import json
from dataclasses import dataclass
from typing import Protocol

from .schemas import Answer, ToolCall


def display_reply(text):
    """Older conversations may contain a copy of the previous internal reply envelope."""
    if not isinstance(text, str) or not text:
        return text
    candidate = text.strip()
    if candidate.startswith(("```json\n", "```\n")) and candidate.endswith("```"):
        candidate = candidate.split("\n", 1)[1][:-3].strip()
    if candidate.startswith("{"):
        try:
            value = json.loads(candidate)
        except ValueError:
            return text
        if (isinstance(value, dict) and set(value) == {"reply", "verified_result", "stop_reason"}
                and isinstance(value["reply"], str) and value["reply"].strip()):
            return value["reply"]
    return text


class InvalidReply(ValueError):
    pass


class ModelProviderError(Exception):
    def __init__(self, code: str, http_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class ToolExchange:
    call: ToolCall
    result: dict | str
    batch_id: str | None = None


@dataclass(frozen=True)
class ModelContext:
    objective: str
    history: tuple[ToolExchange | str, ...]
    previous: tuple[dict, ...] = ()

    @classmethod
    def from_run(cls, run, previous=()):
        history = []
        for event in run["events"]:
            if event["kind"] == "tool_result":
                request = ToolCall(id=event["call_id"], name=event["name"], arguments=event["arguments"])
                history.append(ToolExchange(request, event["output"], event.get("batch_id")))
            elif event["kind"] == "correction":
                if "rejected_call" in event:
                    request = ToolCall.model_validate(event["rejected_call"])
                    history.append(ToolExchange(request, event["detail"]))
                else:
                    history.append(event["detail"])
        return cls(run["objective"], tuple(history), tuple(previous))


class Model(Protocol):
    async def next(self, context: ModelContext) -> ToolCall | tuple[ToolCall, ...] | Answer: ...
