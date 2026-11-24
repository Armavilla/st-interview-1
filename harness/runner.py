import asyncio
import json
from time import monotonic
from uuid import uuid4

from pydantic import ValidationError

from .protocol import InvalidReply, Model, ModelContext, ModelProviderError
from .schemas import Answer, Incident, Receipt, ToolCall
from .storage import DEFAULT_STEPS, public_result
from .tools import SPECS, UnknownService


class DecisionConflict(Exception):
    pass


def elapsed(start):
    return round((monotonic() - start) * 1000)


class Runner:
    def __init__(self, store, model: Model, tools, max_steps=DEFAULT_STEPS, model_timeout=20, tool_timeout=3):
        self.store, self.model, self.tools = store, model, tools
        self.max_steps, self.model_timeout, self.tool_timeout = max_steps, model_timeout, tool_timeout

    def spend(self, run):
        with self.store.connection(True) as db:
            return self.store.spend(db, run)

    async def start(self, objective, conversation=None, emit=None):
        return await self.execute(self.store.create(objective, self.max_steps, conversation), emit)

    def finish(self, run, status, reason, result=None):
        with self.store.connection(True) as db:
            self.store.finish(db, run, status, reason, result)

    async def execute(self, run, emit=None):
        state = self.store.get(run)
        if emit:
            emit("started", run_id=run, conversation_id=state["conversation_id"])
        previous = self.store.turns(state["conversation_id"], before=run)
        while self.spend(run):
            started = monotonic()
            state = self.store.get(run)
            action = None
            try:
                context = ModelContext.from_run(state, previous)
                if emit:
                    emit("model")
                stream = getattr(self.model, "next_streamed", None)
                request = stream(context, emit) if emit and stream else self.model.next(context)
                action = await asyncio.wait_for(request, self.model_timeout)
                if isinstance(action, Answer):
                    self.store.record(run, "model_answer", elapsed_ms=elapsed(started))
                    self.finish(run, "completed", "answered", action.text)
                    break
                calls = action if isinstance(action, tuple) else (action,)
                seen = {e["call_id"] for e in state["events"] if e["kind"] == "model_call"}
                validated = []
                for call in calls:
                    if not isinstance(call, ToolCall):
                        raise InvalidReply("Return a tool call or final answer")
                    if call.id in seen:
                        raise InvalidReply("Use a fresh tool call ID")
                    seen.add(call.id)
                    if call.name not in SPECS:
                        raise InvalidReply("Select a registered tool")
                    spec = SPECS[call.name]
                    if len(calls) > 1 and spec.access != "read":
                        raise InvalidReply("Submit an incident proposal alone after completing read-only calls")
                    validated.append((call, spec.inputs.model_validate(call.arguments)))
                if not validated:
                    raise InvalidReply("Empty tool batch")
                for call, args in validated:
                    self.store.record(run, "model_call", name=call.name, call_id=call.id,
                                      arguments=call.arguments, elapsed_ms=elapsed(started))
            except (InvalidReply, ValidationError) as exc:
                detail = (str(exc.errors(include_input=False, include_url=False))
                          if isinstance(exc, ValidationError) else str(exc))
                if any(e["kind"] == "correction" for e in state["events"]):
                    self.store.record(run, "invalid_reply", detail=detail, elapsed_ms=elapsed(started))
                    self.finish(run, "failed", "invalid_model_response")
                    break
                rejected = ({"rejected_call": action.model_dump()}
                            if isinstance(action, ToolCall) and isinstance(exc, ValidationError) else {})
                self.store.record(run, "correction", detail=detail, elapsed_ms=elapsed(started), **rejected)
                continue
            except TimeoutError:
                self.finish(run, "failed", "model_timeout")
                break
            except ModelProviderError as exc:
                with self.store.connection(True) as db:
                    self.store.event(db, run, "model_error", error=exc.code, http_status=exc.http_status,
                                     elapsed_ms=elapsed(started))
                    self.store.finish(db, run, "failed", exc.code)
                break
            except Exception:
                self.finish(run, "failed", "model_error")
                break

            first, args = validated[0]
            if SPECS[first.name].access == "approval_required":
                if state["steps_used"] >= state["max_steps"]:
                    self.finish(run, "limit_reached", "step_limit")
                    break
                action_id = uuid4().hex
                with self.store.connection(True) as db:
                    db.execute("INSERT INTO approvals(id,run_id,call_id,args) VALUES(?,?,?,?)",
                               (action_id, run, first.id, args.model_dump_json()))
                    db.execute("UPDATE runs SET status='waiting_approval' WHERE id=?", (run,))
                    self.store.event(db, run, "approval_requested", action_id=action_id, call_id=first.id)
                break
            for call, args in validated:
                if emit:
                    emit("tool", name=call.name)
                if not await self.read_tool(run, call, args, first.id if len(validated) > 1 else None):
                    return self.store.get(run)
        return self.store.get(run)

    async def read_tool(self, run, action, args, batch_id=None):
        for attempt in (1, 2):
            if not self.spend(run):
                return False
            started = monotonic()
            info = {"name": action.name, "call_id": action.id, "attempt": attempt}
            try:
                raw = await asyncio.wait_for(self.tools.read(action.name, args), self.tool_timeout)
                output = SPECS[action.name].outputs.model_validate(raw).model_dump()
            except (TimeoutError, ConnectionError) as exc:
                error = "tool_timeout" if isinstance(exc, TimeoutError) else "tool_unavailable"
            except UnknownService:
                error = "service_not_found"
            except ValidationError:
                error = "invalid_tool_output"
            except Exception:
                error = "tool_error"
            else:
                self.store.record(run, "tool_result", **info, arguments=args.model_dump(),
                                  output=output, batch_id=batch_id, elapsed_ms=elapsed(started))
                return True
            self.store.record(run, "tool_error", **info, error=error, elapsed_ms=elapsed(started))
            if attempt == 2 or error not in {"tool_timeout", "tool_unavailable"}:
                self.finish(run, "failed", error)
                return False

    async def decide(self, run, action_id, decision, emit=None):
        with self.store.connection(True) as db:
            row = db.execute("SELECT * FROM approvals WHERE run_id=? AND id=?", (run, action_id)).fetchone()
            if row is None:
                raise KeyError(action_id)
            if row["decision"]:
                if row["decision"] != decision:
                    raise DecisionConflict()
                return public_result(json.loads(row["response"])) if row["response"] else self.store.snapshot(db, run)
            db.execute("UPDATE approvals SET decision=? WHERE id=?", (decision, action_id))
            self.store.event(db, run, "approval_decided", action_id=action_id, decision=decision)
            if decision == "reject":
                self.store.finish(db, run, "completed", "approval_rejected")
            else:
                db.execute("UPDATE runs SET status='running' WHERE id=?", (run,))
                if self.store.spend(db, run):
                    await self.write_approved(db, run, row)
            state = self.store.snapshot(db, run)
            if state["status"] != "running":
                db.execute("UPDATE approvals SET response=? WHERE id=?", (json.dumps(state), action_id))
                return state

        state = await self.execute(run, emit)
        with self.store.connection(True) as db:
            db.execute("UPDATE approvals SET response=? WHERE id=?", (json.dumps(state), action_id))
        return state

    async def write_approved(self, db, run, row):
        args = Incident.model_validate_json(row["args"])
        started = monotonic()
        info = {"name": "create_incident", "call_id": row["call_id"], "attempt": 1}
        db.execute("SAVEPOINT incident_write")
        try:
            raw = await asyncio.wait_for(self.tools.create_incident(args, row["id"], db), self.tool_timeout)
            output = Receipt.model_validate(raw)
        except TimeoutError:
            error = "incident_outcome_unknown"
        except ValidationError:
            error = "invalid_tool_output"
        except Exception:
            error = "tool_error"
        else:
            saved = db.execute("SELECT id FROM incidents WHERE action_id=?", (row["id"],)).fetchone()
            if saved is not None and saved["id"] == output.incident_id:
                db.execute("RELEASE incident_write")
                self.store.event(db, run, "tool_result", **info, arguments=args.model_dump(),
                                 output=output.model_dump(), elapsed_ms=elapsed(started))
                return
            error = "invalid_tool_output"
        db.execute("ROLLBACK TO incident_write")
        db.execute("RELEASE incident_write")
        self.store.event(db, run, "tool_error", **info, error=error, elapsed_ms=elapsed(started))
        self.store.finish(db, run, "failed", error)
