import json

import httpx
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.model import DeepSeekModel


HEADERS = {"Accept": "application/x-ndjson"}


def events(response):
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.iter_lines()]


def chunks(parts):
    lines = ["data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": end}]})
             for delta, end in parts]
    return httpx.Response(200, text="\n\n".join([*lines, "data: [DONE]"]) + "\n\n")


def test_stream_tool_and_text(tmp_path):
    def respond(request):
        payload = json.loads(request.content)
        assert payload["stream"] is True
        if payload["messages"][-1]["role"] == "tool":
            return chunks([({"content": "状态"}, None), ({"content": "正常"}, "stop")])
        return chunks([
            ({"tool_calls": [{"index": 0, "id": "t1", "type": "function",
                             "function": {"name": "get_service_status", "arguments": '{"service_'}}]}, None),
            ({"tool_calls": [{"index": 0, "function": {"arguments": 'name":"catalog-api"}'}}]}, "tool_calls"),
        ])
    model = DeepSeekModel("test", "test", httpx.MockTransport(respond))
    with TestClient(create_app(tmp_path / "chat.db", model=model)) as client:
        log = events(client.post("/runs", json={"objective": "查状态"}, headers=HEADERS))
        assert log[0]["type"] == "started"
        assert [e["text"] for e in log if e["type"] == "delta"] == ["状态", "正常"]
        assert any(e.get("name") == "get_service_status" for e in log)
        assert log[-1]["run"]["reply"] == "状态正常" and log[-1]["run"]["steps_used"] == 3


def test_historical_and_streamed_envelopes(tmp_path):
    envelope = json.dumps({"reply": "哪个服务？", "verified_result": "pretend", "stop_reason": "needs_clarification"},
                          ensure_ascii=False)
    def respond(request):
        messages = json.loads(request.content)["messages"]
        assert all("verified_result" not in m["content"] for m in messages if m["role"] == "assistant")
        return chunks([({"content": envelope[:8]}, None), ({"content": envelope[8:]}, "stop")])
    model = DeepSeekModel("test", "test", httpx.MockTransport(respond))
    with TestClient(create_app(tmp_path / "chat.db", model=model)) as client:
        first = events(client.post("/runs", json={"objective": "状态？"}, headers=HEADERS))[-1]["run"]
        store = client.app.state.runner.store
        with store.connection(True) as db:
            db.execute("UPDATE runs SET result=? WHERE id=?", (envelope, first["run_id"]))
        url = "/conversations/" + first["conversation_id"]
        turn = client.get(url).json()["turns"][0]
        assert turn["reply"] == "哪个服务？" and turn["observations"] == []
        log = events(client.post(url + "/messages", json={"message": "嗯？"}, headers=HEADERS))
        assert log[-1]["run"]["reply"] == "哪个服务？"
        assert all("verified_result" not in e.get("text", "") for e in log)


def test_incomplete_tool_stream(tmp_path):
    def respond(request):
        return chunks([({"tool_calls": [{"index": 0, "id": "x", "type": "function",
            "function": {"name": "create_incident", "arguments": "{}"}}]}, None)])
    model = DeepSeekModel("test", "test", httpx.MockTransport(respond))
    with TestClient(create_app(tmp_path / "chat.db", model=model)) as client:
        run = events(client.post("/runs", json={"objective": "查一下"}, headers=HEADERS))[-1]["run"]
        assert run["stop_reason"] == "invalid_model_response" and run["steps_used"] == 2
        assert run["pending_action"] is None and run["incident_ids"] == []


def test_stream_approval_and_conflicts(tmp_path):
    with TestClient(create_app(tmp_path / "chat.db")) as client:
        start = client.post("/runs", headers=HEADERS, json={"objective": "Investigate checkout-api and propose an incident"})
        run = events(start)[-1]["run"]
        assert run["status"] == "waiting_approval" and run["incident_ids"] == []
        url = "/conversations/" + run["conversation_id"] + "/messages"
        assert client.post(url, headers=HEADERS, json={"message": "go"}).status_code == 409
        approval = "/runs/" + run["run_id"] + "/approvals/" + run["pending_action"]["action_id"]
        for _ in range(2):
            receipt = events(client.post(approval, headers=HEADERS, json={"decision": "approve"}))[-1]["run"]
            assert len(receipt["incident_ids"]) == 1
        assert client.post(approval, headers=HEADERS, json={"decision": "reject"}).status_code == 409
        assert client.post("/conversations/missing/messages", headers=HEADERS, json={"message": "?"}).status_code == 404
