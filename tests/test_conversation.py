import json

import httpx
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.model import DeepSeekModel


def test_chat_entry_point(tmp_path):
    with TestClient(create_app(tmp_path / "chat.db")) as client:
        home = client.get("/")
        assert home.status_code == 200 and home.headers["content-type"].startswith("text/html")
        assert "运维助手" in home.text
        assert client.get("/info").json() == {
            "backend": "demo", "model": "demo",
            "services": ["catalog-api", "checkout-api"], "data_source": "synthetic",
        }


def test_clarification_and_batch_after_restart(tmp_path):
    path = tmp_path / "chat.db"
    questions = []

    def respond(request):
        msgs = json.loads(request.content)["messages"]
        last = msgs[-1]
        if last["role"] == "tool":
            assert [m["tool_call_id"] for m in msgs[-2:]] == ["a", "b"]
            assert [t["id"] for t in msgs[-3]["tool_calls"]] == ["a", "b"]
            reason, message = "stop", {"content": "checkout-api 降级，catalog-api 健康。"}
        elif last["content"] == "两个都查一下":
            assert any(m["role"] == "user" and m["content"] == "我操，服务是不是挂了" for m in msgs)
            assert any(m["role"] == "assistant" and "哪个服务" in m["content"] for m in msgs)
            calls = [{"type": "function", "id": ident, "function": {
                "name": "get_service_status", "arguments": json.dumps({"service_name": name})}}
                for ident, name in [("a", "catalog-api"), ("b", "checkout-api")]]
            reason, message = "tool_calls", {"tool_calls": calls, "content": "我先查两个服务。"}
        else:
            questions.append(last["content"])
            reason, message = "stop", {"content": "你指哪个服务？"}
        return httpx.Response(200, json={"choices": [{"finish_reason": reason,
            "message": {"role": "assistant", **message}}]})

    model = DeepSeekModel("test", "test", httpx.MockTransport(respond))
    with TestClient(create_app(path, model=model)) as client:
        first = client.post("/runs", json={"objective": "我操，服务是不是挂了"}).json()
        assert first["reply"] == "你指哪个服务？"
        thread = first["conversation_id"]
    with TestClient(create_app(path, model=model)) as client:
        second = client.post(f"/conversations/{thread}/messages", json={"message": "两个都查一下"}).json()
        assert second["conversation_id"] == thread and second["run_id"] != first["run_id"]
        assert second["steps_used"] == 4 and second["status"] == "completed"
        assert "catalog-api" in second["result"] and "checkout-api" in second["result"]
        assert len(client.get(f"/conversations/{thread}").json()["turns"]) == 2
        other = client.post("/runs", json={"objective": "新问题"}).json()
        assert len(client.get(f"/conversations/{other['conversation_id']}").json()["turns"]) == 1
        assert questions == ["我操，服务是不是挂了", "新问题"]


def test_followup_cannot_bypass_approval(tmp_path):
    with TestClient(create_app(tmp_path / "demo.db")) as client:
        run = client.post("/runs", json={"objective": "Investigate checkout-api and propose an incident"}).json()
        url = "/conversations/" + run["run_id"] + "/messages"
        assert client.post(url, json={"message": "我同意"}).status_code == 409
        assert client.get("/runs/" + run["run_id"]).json()["incident_ids"] == []
        action = run["pending_action"]["action_id"]
        client.post(f"/runs/{run['run_id']}/approvals/{action}", json={"decision": "reject"})
        assert client.post(url, json={"message": "再查一下"}).status_code == 201
        assert client.post("/conversations/unknown/messages", json={"message": "hi"}).status_code == 404
