import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.model import call
from harness.schemas import Answer
from harness.tools import MockTools


HEALTH = {"objective": "Check catalog-api"}
FAULT = {"objective": "Investigate checkout-api and propose an incident if required."}


def start(client, objective=FAULT):
    response = client.post("/runs", json=objective)
    assert response.status_code == 201, response.text
    return response.json()


def endpoint(run):
    return f"/runs/{run['run_id']}/approvals/{run['pending_action']['action_id']}"


@pytest.fixture
def database(tmp_path):
    return tmp_path / "state.db"


def test_healthy(database):
    with TestClient(create_app(database)) as client:
        run = start(client, HEALTH)
        assert (run["status"], run["steps_used"], run["incident_ids"]) == ("completed", 3, [])
        assert "healthy" in run["result"]
        assert client.get("/runs/" + run["run_id"]).json() == run
        assert client.get("/runs/unknown").status_code == 404
        assert client.post("/runs", json={"objective": " "}).status_code == 422
        assert [e["seq"] for e in run["events"]] == sorted(e["seq"] for e in run["events"])


def test_restart_and_concurrent_approval(database):
    tools = MockTools()
    tools.create_incident = AsyncMock(wraps=tools.create_incident)
    with TestClient(create_app(database, tools=tools)) as client:
        run = start(client)
        assert run["status"] == "waiting_approval" and run["incident_ids"] == []
        tools.create_incident.assert_not_awaited()
    with TestClient(create_app(database, tools=tools)) as client:
        url = endpoint(run)
        assert client.post(url, json={"decision": "approve", "severity": "P1"}).status_code == 422
        assert client.post(url.replace(run["run_id"], "wrong"), json={"decision": "approve"}).status_code == 404
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda _: client.post(url, json={"decision": "approve"}), range(2)))
        assert all(r.status_code == 200 for r in responses)
        final = client.post(url, json={"decision": "approve"}).json()
        assert final["status"] == "completed"
        assert len(final["incident_ids"]) == 1 and final["incident_ids"][0] in final["result"]
        assert all(r.json()["incident_ids"] == final["incident_ids"] for r in responses)
        tools.create_incident.assert_awaited_once()
        assert tools.create_incident.await_args.args[0].model_dump() == run["pending_action"]["arguments"]
        assert client.post(url, json={"decision": "reject"}).status_code == 409


def test_rejection(database):
    tools = MockTools()
    tools.create_incident = AsyncMock()
    with TestClient(create_app(database, tools=tools)) as client:
        url = endpoint(start(client))
        response = client.post(url, json={"decision": "reject"}).json()
        assert response["stop_reason"] == "approval_rejected" and response["incident_ids"] == []
        assert "degraded" in response["result"]
        assert client.post(url, json={"decision": "reject"}).json() == response
        tools.create_incident.assert_not_awaited()


@pytest.mark.parametrize("errors, expected, attempts", [
    ([TimeoutError(), {"service_name": "catalog-api", "status": "healthy", "duration_minutes": 60}], "answered", 2),
    ([ConnectionError(), ConnectionError()], "tool_unavailable", 2),
    ([RuntimeError()], "tool_error", 1),
    ([{"status": "purple"}], "invalid_tool_output", 1),
])
def test_read_errors(database, errors, expected, attempts):
    tools = MockTools()
    tools.read = AsyncMock(side_effect=errors)
    with TestClient(create_app(database, tools=tools)) as client:
        run = start(client, HEALTH)
        assert run["stop_reason"] == expected and tools.read.await_count == attempts
        assert any(e["kind"] == "tool_error" and "elapsed_ms" in e for e in run["events"])


async def hang(*args):
    await asyncio.sleep(5)


def test_actual_timeouts(database):
    tools = MockTools()
    tools.read = AsyncMock(side_effect=hang)
    with TestClient(create_app(database, tools=tools, tool_timeout=.01)) as client:
        assert start(client, HEALTH)["stop_reason"] == "tool_timeout"
        assert tools.read.await_count == 2


def test_model_timeout(database):
    model = AsyncMock()
    model.next.side_effect = hang
    with TestClient(create_app(database, model=model, model_timeout=.01)) as client:
        assert start(client)["stop_reason"] == "model_timeout"


def test_write_timeout_never_retries(database):
    tools = MockTools()
    tools.create_incident = AsyncMock(side_effect=hang)
    with TestClient(create_app(database, tools=tools, tool_timeout=.01)) as client:
        url = endpoint(start(client))
        for _ in range(2):
            assert client.post(url, json={"decision": "approve"}).json()["stop_reason"] == "incident_outcome_unknown"
        tools.create_incident.assert_awaited_once()


def test_unknown_service(database):
    with TestClient(create_app(database)) as client:
        run = start(client, {"objective": "check missing-api"})
        assert run["stop_reason"] == "service_not_found" and run["steps_used"] == 2


@pytest.mark.parametrize("bad", [None, call("missing"), call("create_incident", severity="P9")])
def test_correction(database, bad):
    model = AsyncMock()
    model.next.side_effect = [bad, Answer(text="Need more information")]
    with TestClient(create_app(database, model=model)) as client:
        run = start(client)
        assert run["stop_reason"] == "answered" and run["steps_used"] == 2
        assert all(e["kind"] != "tool_result" for e in run["events"])


def test_persistent_bad_response(database):
    model = AsyncMock()
    model.next.return_value = {}
    with TestClient(create_app(database, model=model)) as client:
        assert start(client)["stop_reason"] == "invalid_model_response"
        assert model.next.await_count == 2


def test_infinite_queries(database):
    model = AsyncMock()
    model.next.side_effect = lambda state: call("get_service_status", service_name="catalog-api")
    with TestClient(create_app(database, model=model)) as client:
        run = start(client)
        assert run["status"] == "limit_reached" and run["steps_used"] == 12
        assert model.next.await_count == 6


def test_no_budget_for_approved_write(database):
    tools = MockTools()
    tools.create_incident = AsyncMock()
    with TestClient(create_app(database, tools=tools, max_steps=5)) as client:
        run = start(client)
        assert run["status"] == "limit_reached" and run["pending_action"] is None
        assert not any(e["kind"] == "approval_requested" for e in run["events"])
        tools.create_incident.assert_not_awaited()


def test_legacy_pending_approval_still_checks_budget(database):
    tools = MockTools()
    tools.create_incident = AsyncMock()
    with TestClient(create_app(database, tools=tools)) as client:
        run = start(client)
        with sqlite3.connect(database) as db:
            db.execute("UPDATE runs SET max_steps=steps_used WHERE id=?", (run["run_id"],))
        final = client.post(endpoint(run), json={"decision": "approve"}).json()
        assert final["status"] == "limit_reached" and final["steps_used"] == 5
        tools.create_incident.assert_not_awaited()


@pytest.mark.parametrize("initial,after_restart,expected", [
    (6, 12, "limit_reached"), (12, 1, "completed"),
])
def test_run_keeps_original_budget(database, initial, after_restart, expected):
    with TestClient(create_app(database, max_steps=initial)) as client:
        run = start(client)
    with TestClient(create_app(database, max_steps=after_restart)) as client:
        final = client.post(endpoint(run), json={"decision": "approve"}).json()
        assert final["status"] == expected
        assert final["max_steps"] == initial
        assert len(final["incident_ids"]) == 1
        assert final["incident_ids"][0] in final["result"]


def test_existing_database_gets_original_default(database):
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE runs(id TEXT PRIMARY KEY, objective TEXT, status TEXT, "
                   "steps_used INTEGER DEFAULT 0, result TEXT, stop_reason TEXT)")
        db.execute("INSERT INTO runs VALUES ('legacy','check service','completed',3,'healthy','answered')")
    with TestClient(create_app(database, max_steps=4)) as client:
        old = client.get("/runs/legacy").json()
        assert (old["max_steps"], old["steps_used"], old["model_summary"]) == (12, 3, "healthy")
        assert start(client, HEALTH)["max_steps"] == 4


def test_unverified_model_text_is_separate_from_result(database):
    model = AsyncMock()
    model.next.return_value = Answer(text="Incident INC-invented was created.")
    with TestClient(create_app(database, model=model)) as client:
        run = start(client)
        assert run["incident_ids"] == [] and "INC-invented" not in run["result"]
        assert "INC-invented" in run["model_summary"]
        assert run == client.get("/runs/" + run["run_id"]).json()


@pytest.mark.parametrize("failure,reason", [
    ("invalid", "invalid_tool_output"), ("mismatch", "invalid_tool_output"),
    ("error", "tool_error"), ("timeout", "incident_outcome_unknown"),
])
def test_failed_local_write_rolls_back_and_keeps_decision(database, failure, reason):
    tools = MockTools()
    insert = tools.create_incident
    async def fail_after_insert(*args):
        await insert(*args)
        if failure == "timeout":
            await hang()
        if failure == "error":
            raise RuntimeError()
        return {"incident_id": "INC-wrong"} if failure == "mismatch" else {}
    tools.create_incident = AsyncMock(side_effect=fail_after_insert)
    with TestClient(create_app(database, tools=tools, tool_timeout=.05)) as client:
        url = endpoint(start(client))
        final = client.post(url, json={"decision": "approve"}).json()
        assert final["stop_reason"] == reason and final["incident_ids"] == []
        assert client.post(url, json={"decision": "approve"}).json() == final
        assert any(e["kind"] == "approval_decided" for e in final["events"])
        tools.create_incident.assert_awaited_once()


@pytest.mark.parametrize("objective,expected", [
    ("Check checkout-api. Do not propose or create an incident.", "completed"),
    ("Check checkout-api and report whether an incident already exists.", "completed"),
    ("检查 checkout-api，不要建单。", "completed"),
    ("检查 checkout-api，符合手册条件时申请建单。", "waiting_approval"),
    ("Investigate checkout-api and propose an incident if required.", "waiting_approval"),
])
def test_demo_incident_intent(database, objective, expected):
    with TestClient(create_app(database)) as client:
        run = start(client, {"objective": objective})
        assert run["status"] == expected and run["incident_ids"] == []
