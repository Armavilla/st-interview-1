# Operations Agent Harness

Chat with an operations assistant, inspect the evidence behind its answers, and approve or decline a proposed incident. Choose DeepSeek for free-form conversation or the offline demo to exercise the harness without credentials. Both use synthetic operations data.

## Run locally

Use Python 3.11 or newer. From the project root on Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn harness.api:app --host 127.0.0.1 --port 8000
```

On macOS or Linux, use `python3 -m venv .venv` and replace `.venv\Scripts\python` with `.venv/bin/python` in the commands.

## Chat in the browser

Open [the local chat](http://127.0.0.1:8000/). The header shows the active backend. In the default offline mode, enter `Investigate checkout-api and propose an incident if required.` The page displays a P2 proposal. Approve it to see the incident ID, or decline it to finish with zero incidents.

In DeepSeek mode try “服务是不是挂了？”, then “我说的是下单服务”, then “持续多久了？”. Ask “两个服务都看一下” to compare them. Press Enter to send or Shift+Enter for a newline. Expand “工具核实的结果” for observations and a link to the execution trace. If you request an incident, review its title, description and severity before approving or declining it on the page.

Refresh to resume, copy the page URL to revisit the conversation on this server, or use “最近对话” in the same browser. “新对话” starts with fresh context. SQLite stores the full history. Each new message gives the model the previous 12 turns at most.

In live mode, the page displays text as DeepSeek generates it and indicates when the assistant is checking a service or searching a runbook. Expand the observations under a reply to compare it with tool output. Reloading during generation reconnects to the saved run status and displays the completed answer when available.

API clients can start with `POST /runs` and continue with `POST /conversations/{conversation_id}/messages` using `{"message":"持续多久了？"}`. Read `reply` for the assistant's natural-language answer and `GET /conversations/{conversation_id}` for the conversation. Each turn has its own run ID, trace and budget. A follow-up returns HTTP 409 while the current run is executing or awaiting approval.

For API clients, the message and approval POST endpoints also support `Accept: application/x-ndjson`. Each line is a JSON event. A new run emits `started` with its IDs, followed by model activity, text deltas or tool activity, and a final `done` event containing the run snapshot. The default response is a single JSON snapshot.

## Try the three scenarios

Import [the Postman collection](postman/operations-harness.postman_collection.json) and run it from top to bottom with the service running. Its collection variable `base_url` defaults to `http://127.0.0.1:8000`. The collection saves run and action IDs automatically.

1. Check `catalog-api`. It is healthy, so the run finishes with zero incidents.
2. Investigate `checkout-api` and request an incident if the runbook threshold is met. The snapshot reports 15 minutes of degradation against a 10-minute threshold. Inspect the proposed P2 incident, approve it, then repeat approval and compare the incident IDs.
3. Start another checkout investigation and reject the proposal. The trace retains the observations and the run ends with zero incidents.

Interactive API documentation is available at [local Swagger UI](http://127.0.0.1:8000/docs). Submit `{"objective":"Investigate checkout-api and propose an incident if required."}` to `POST /runs`. Inspect `GET /runs/{run_id}`. To decide on a pending proposal, send `{"decision":"approve"}` or `{"decision":"reject"}` to `POST /runs/{run_id}/approvals/{action_id}`. Read the run's `status` and `stop_reason` to distinguish successful execution, a pending decision, a step limit and a tool failure. The HTTP status describes the API request.

## Run the automated tests

From the repository root, run the suite in the project environment with the default offline configuration:

```powershell
.venv\Scripts\python -m pytest -q
```

The tests use temporary databases and simulated provider responses. They cover conversation continuity, successful tool use, tool failures, malformed responses, approvals and execution limits.

## How the offline demo works

`MODEL_BACKEND=demo` selects a deterministic Python function for the model's turn. It reads the objective and previous tool results and returns the same `ToolCall` or `Answer` objects as the live adapter. The runner validates each request, executes the registered tool, records its result and calls the function again.

For the checkout example, the first turn requests the status. The tool reads the synthetic snapshot showing 15 minutes of degradation. The second turn requests the runbook. The demo recognizes OPS-17 and has its 10-minute P2 rule encoded in Python. The next turn proposes a P2 incident and the runner pauses for approval. On approval the mock incident tool inserts a row in SQLite and returns its ID. The demo then reads that receipt to finish. No model-provider request is made.

The demo recognizes explicit request templates such as `Investigate checkout-api and propose an incident if required.` and `检查 checkout-api，符合手册条件时申请建单。`. With other wording, the demo can report observations and leaves incident creation to an explicit request. Live mode uses the model provider to interpret free-form wording. Both modes use the same synthetic tools, approvals, database and step budget.

For operational decisions use `result` and `incident_ids`. The harness builds these from recorded observations and committed incident rows, even when the step budget expires after a successful write. The separate `model_summary` contains unverified model text. For a live provider error, inspect the run's stop reason and the trace event's HTTP status.

## Model configuration

| Process environment variable | Use |
| --- | --- |
| `MODEL_BACKEND` | Defaults to `demo` for deterministic offline examples. Set to `deepseek` for live calls. |
| `DEEPSEEK_API_KEY` | Required for live calls. Supply your DeepSeek key in the process environment. |
| `DEEPSEEK_MODEL` | Optional. Defaults to `deepseek-flash`. Overrides must support Chat Completions, non-thinking mode and function calling. |

Set variables before starting or restarting the service. With your key already in the environment, use `$env:MODEL_BACKEND = 'deepseek'` in PowerShell or `export MODEL_BACKEND=deepseek` in a POSIX shell. Live mode sends the objective, recent conversation context and tool results to DeepSeek and consumes that account's API quota. The adapter uses non-thinking mode and supports multiple read calls in one model response. An incident proposal must be issued alone. Tool execution and human approval run locally. The offline suite uses simulated DeepSeek HTTP responses. [.env.example](.env.example) · [DeepSeek tool calling](https://api-docs.deepseek.com/guides/tool_calls/)

## Assessment materials

- [Design report](docs/design.md) covers the execution loop, database, safety controls, limits and future work.
- [Service snapshots](data/services.json) and [internal runbooks](data/knowledge_base.json) are synthetic assessment data.
- [Tests](tests/) run against temporary databases and simulated model responses. The API stores its local database in `var/harness.sqlite3` and emits trace events through the Uvicorn logger. Run one Uvicorn worker.
