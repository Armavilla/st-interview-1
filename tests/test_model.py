import json

import httpx
import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.model import DeepSeekModel, configured_model


def tool_call(name, arguments, identifier="call_1"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def response(reason, **message):
    return httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": {"role": "assistant", **message}}]})


def test_live_protocol_offline(tmp_path):
    requests = []

    def handle(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer test"
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["messages"][0]["role"] == "system"
        assert {t["function"]["name"] for t in payload["tools"]} == {
            "search_knowledge_base", "get_service_status", "create_incident"}
        assert all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object"
                   for t in payload["tools"])
        if len(requests) == 1:
            return httpx.Response(200, text="malformed")
        if len(requests) == 2:
            return response("tool_calls", tool_calls=[tool_call("get_service_status", {"service_name": "catalog-api"})])
        assistant, result = payload["messages"][-2:]
        assert assistant["tool_calls"][0]["id"] == result["tool_call_id"] == "call_1"
        assert result["role"] == "tool" and json.loads(result["content"])["status"] == "healthy"
        return response("stop", content="Healthy.")

    model = DeepSeekModel("test", "test", httpx.MockTransport(handle))
    with TestClient(create_app(tmp_path / "test.db", model=model)) as client:
        run = client.post("/runs", json={"objective": "check catalog-api"}).json()
        assert "healthy" in run["result"] and len(requests) == 3
        assert run["model_summary"] == "Healthy."
        assert any(e["kind"] == "correction" for e in run["events"])


def test_live_config(monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        configured_model()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    assert configured_model().model == "deepseek-flash"
    monkeypatch.setenv("DEEPSEEK_MODEL", "override")
    assert configured_model().model == "override"


@pytest.mark.parametrize("code,reason", [
    (401, "model_authentication_error"), (403, "model_permission_denied"),
    (429, "model_rate_limited"), (503, "model_unavailable"), (400, "model_request_error"),
])
def test_provider_errors_are_actionable_and_sanitized(tmp_path, code, reason):
    def handle(request):
        return httpx.Response(code, text="private upstream detail")
    model = DeepSeekModel("private-test-key", "test", httpx.MockTransport(handle))
    with TestClient(create_app(tmp_path / "test.db", model=model)) as client:
        run = client.post("/runs", json={"objective": "Check catalog-api"}).json()
        assert run["status"] == "failed" and run["stop_reason"] == reason
        assert any(e.get("http_status") == code and e.get("error") == reason for e in run["events"])
        trace = json.dumps(run)
        assert "private upstream detail" not in trace and "private-test-key" not in trace


def test_invalid_tool_arguments_link_to_original_call(tmp_path):
    def handle(request):
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "user":
            return response("tool_calls", tool_calls=[tool_call("create_incident", {
                "title": "Issue", "description": "Observed", "severity": "P9"}, "bad-input")])
        rejected = messages[-2]["tool_calls"][0]
        assert json.loads(rejected["function"]["arguments"])["severity"] == "P9"
        feedback = messages[-1]
        assert feedback["tool_call_id"] == rejected["id"] == "bad-input"
        assert "severity" in json.loads(feedback["content"])["error"]
        return response("stop", content="No incident created.")

    model = DeepSeekModel("test", "test", httpx.MockTransport(handle))
    with TestClient(create_app(tmp_path / "test.db", model=model)) as client:
        run = client.post("/runs", json={"objective": "investigate checkout-api"}).json()
        assert run["status"] == "completed" and run["incident_ids"] == []


@pytest.mark.parametrize("reply", [
    response("tool_calls", tool_calls=[tool_call("create_incident", {}), tool_call("unknown", {}, "call_2")]),
    response("tool_calls", tool_calls=[{"id": "incomplete", "type": "function",
                                      "function": {"name": "create_incident", "arguments": "{"}}]),
    response("length", content="Incomplete"),
    response({}, content="Bad finish reason"),
    response("stop", tool_calls=3, content="Bad tool list"),
    response("tool_calls", tool_calls=[
        tool_call("get_service_status", {"service_name": "catalog-api"}),
        tool_call("create_incident", {"title": "Incident", "description": "Observed", "severity": "P2"}, "call_2"),
    ]),
    httpx.Response(200, json={"choices": []}),
])
def test_unusable_deepseek_reply_is_bounded(tmp_path, reply):
    model = DeepSeekModel("test", "test", httpx.MockTransport(lambda request: reply))
    with TestClient(create_app(tmp_path / "test.db", model=model)) as client:
        run = client.post("/runs", json={"objective": "Investigate checkout-api"}).json()
        assert run["stop_reason"] == "invalid_model_response" and run["steps_used"] == 2
        assert run["pending_action"] is None and run["incident_ids"] == []


def test_live_approval_resume_replays_receipt(tmp_path):
    def handle(request):
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "user":
            return response("tool_calls", tool_calls=[tool_call("create_incident", {
                "title": "Checkout outage", "description": "Investigate checkout", "severity": "P2"})])
        assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "call_1"
        receipt = json.loads(messages[-1]["content"])
        return response("stop", content="Created " + receipt["incident_id"])

    model = DeepSeekModel("test", "test", httpx.MockTransport(handle))
    with TestClient(create_app(tmp_path / "test.db", model=model)) as client:
        run = client.post("/runs", json={"objective": "Open an incident"}).json()
        assert run["status"] == "waiting_approval" and not run["incident_ids"]
        url = f"/runs/{run['run_id']}/approvals/{run['pending_action']['action_id']}"
        final = client.post(url, json={"decision": "approve"}).json()
        assert final["status"] == "completed" and final["incident_ids"][0] in final["model_summary"]
