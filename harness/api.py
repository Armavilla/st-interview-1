import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .model import DeepSeekModel, configured_model
from .runner import DecisionConflict, Runner
from .schemas import Decision, Objective, UserMessage
from .storage import ConversationBusy, Store
from .tools import MockTools


def create_app(path=Path("var/harness.sqlite3"), model=None, tools=None, **limits):
    workers = set()

    @asynccontextmanager
    async def lifespan(app):
        store = Store(path)
        store.recover()
        active_tools = tools or MockTools()
        app.state.runner = Runner(store, model or configured_model(tuple(active_tools.services)), active_tools, **limits)
        yield
        if workers:
            await asyncio.gather(*workers)

    api = FastAPI(title="Operations Agent Harness", lifespan=lifespan)
    api.mount("/assets", StaticFiles(directory=Path(__file__).with_name("static")), name="assets")

    async def respond(http, operation, *args, status=200):
        if "application/x-ndjson" not in http.headers.get("accept", ""):
            return await asyncio.to_thread(lambda: asyncio.run(operation(*args)))

        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()

        def emit(kind, **fields):
            loop.call_soon_threadsafe(queue.put_nowait, {"type": kind, **fields})

        def produce():
            try:
                run = asyncio.run(operation(*args, emit=emit))
                emit("done", run=run)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)

        # Completing a bounded turn is independent of the browser connection.
        worker = asyncio.create_task(asyncio.to_thread(produce))
        workers.add(worker)
        worker.add_done_callback(workers.discard)
        first = await queue.get()
        if isinstance(first, Exception):
            raise first

        async def lines():
            item = first
            while True:
                if isinstance(item, Exception):
                    yield '{"type":"error"}\n'
                    return
                yield json.dumps(item, ensure_ascii=False) + "\n"
                if item["type"] == "done":
                    return
                item = await queue.get()

        return StreamingResponse(lines(), status_code=status, media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @api.get("/", include_in_schema=False)
    def chat():
        return FileResponse(Path(__file__).with_name("chat.html"))

    @api.get("/info")
    def info():
        runner = api.state.runner
        live = isinstance(runner.model, DeepSeekModel)
        return {"backend": "deepseek" if live else "demo", "model": runner.model.model if live else "demo",
                "services": sorted(runner.tools.services), "data_source": "synthetic"}

    @api.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str):
        try:
            return {"conversation_id": conversation_id, "turns": api.state.runner.store.turns(conversation_id)}
        except KeyError:
            raise HTTPException(404, "Unknown conversation")

    @api.post("/conversations/{conversation_id}/messages", status_code=201)
    async def followup(conversation_id: str, request: UserMessage, http: Request):
        try:
            return await respond(http, api.state.runner.start, request.message, conversation_id, status=201)
        except KeyError:
            raise HTTPException(404, "Unknown conversation")
        except ConversationBusy as exc:
            raise HTTPException(409, f"Current turn is {exc.args[0]}")

    @api.post("/runs", status_code=201)
    async def start(request: Objective, http: Request):
        return await respond(http, api.state.runner.start, request.objective, status=201)

    @api.get("/runs/{run_id}")
    def inspect(run_id: str):
        try:
            return api.state.runner.store.get(run_id)
        except KeyError:
            raise HTTPException(404, "Unknown run")

    @api.post("/runs/{run_id}/approvals/{action_id}")
    async def approve(run_id: str, action_id: str, request: Decision, http: Request):
        try:
            return await respond(http, api.state.runner.decide, run_id, action_id, request.decision)
        except KeyError:
            raise HTTPException(404, "Unknown action for this run")
        except DecisionConflict:
            raise HTTPException(409, "A different decision has already been recorded")

    return api


app = create_app()
