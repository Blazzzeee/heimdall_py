import os
import hashlib
import hmac
import json
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("HEIMDALL_ALLOW_DEFAULTS", "1")
os.environ.setdefault("INFRA_API_KEY", "heimdall")
os.environ.setdefault("WEBHOOK_SECRET", "super-secret-key")

from api import app, INFRA_API_KEY
from db import SessionLocal, Node, ServiceInstance, Operation, init_db


@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    db = SessionLocal()
    db.query(Operation).delete()
    db.query(ServiceInstance).delete()
    db.query(Node).delete()
    db.commit()
    db.close()

@pytest.fixture(autouse=True)
def mock_agent_command(monkeypatch):
    async def mock_cmd(*args, **kwargs):
        return {"status": "accepted"}
    monkeypatch.setattr("app.ops.send_agent_command", mock_cmd)

client = TestClient(app)


def sign_payload(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(INFRA_API_KEY.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return body, {"x-signature": f"sha256={digest}", "Content-Type": "application/json"}


# ── Webhook tests ────────────────────────────────────────────────────────────

def test_webhook_deploy():
    # Setup: register a node first
    db = SessionLocal()
    node = Node(name="test-node", uuid="test-uuid", host="localhost")
    db.add(node)
    db.commit()
    db.close()

    payload = {"action": "deploy", "service": "api", "version": "v1"}
    body, headers = sign_payload(payload)

    r = client.post("/webhook", data=body, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"status": "accepted"}

    db = SessionLocal()
    ops = db.query(Operation).all()
    assert len(ops) == 1
    assert ops[0].type == "deploy"
    db.close()


def test_webhook_register():
    payload = {
        "action": "register",
        "service": "node-v2",
        "metadata": {"name": "node-v2", "host": "http://localhost:8002"},
    }
    body, headers = sign_payload(payload)

    r = client.post("/webhook", data=body, headers=headers)
    assert r.status_code == 200

    db = SessionLocal()
    node = db.query(Node).filter(Node.name == "node-v2").first()
    assert node is not None
    assert node.host == "http://localhost:8002"
    db.close()


def test_webhook_invalid_signature():
    payload = {"action": "deploy", "service": "api"}
    body = json.dumps(payload).encode("utf-8")

    r = client.post("/webhook", data=body, headers={"x-signature": "sha256=bad"})
    assert r.status_code == 401


def test_webhook_missing_header():
    payload = {"action": "deploy", "service": "api"}
    body = json.dumps(payload).encode("utf-8")

    r = client.post("/webhook", data=body)
    assert r.status_code == 422


# ── Infra control tests ─────────────────────────────────────────────────────

API_KEY_HEADER = {"X-API-Key": INFRA_API_KEY}


def test_declare_service():
    db = SessionLocal()
    db.add(Node(name="declare-node", uuid="uuid-declare", host="http://localhost:8004"))
    db.commit()
    db.close()

    # Declare the service
    r = client.post("/services", json={
        "service": "declared-svc",
        "node_name": "declare-node",
        "flake": "path:/test",
    }, headers=API_KEY_HEADER)
    assert r.status_code == 200
    
    # Add deploy command to the declared service
    db = SessionLocal()
    svc = db.query(ServiceInstance).filter(ServiceInstance.name == "declared-svc").first()
    svc.commands = {"deploy": "default"}
    db.commit()
    db.close()

    # Deploy without node_name
    r = client.post("/deploy", json={
        "service": "declared-svc",
        "version": "v2",
    }, headers=API_KEY_HEADER)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    
    # Verify the operation used the declared config
    op_id = data["operation_id"]
    db = SessionLocal()
    op = db.query(Operation).filter_by(id=op_id).first()
    assert op.metadata_json["flake"] == "path:/test"
    db.close()

def test_deploy_endpoint_with_node():
    db = SessionLocal()
    node = Node(name="deploy-node", uuid="uuid-1", host="http://localhost:8001")
    db.add(node)
    db.commit()
    db.refresh(node)
    db.add(ServiceInstance(
        node_id=node.id,
        name="api-gateway",
        service_uuid="api-gateway",
        flake="github:org/repo#api",
        commands={"deploy": "api"},
    ))
    db.commit()
    db.close()

    r = client.post("/deploy", json={
        "service": "api-gateway",
        "node_name": "deploy-node",
        "commands": ["nix run"],
        "version": "v1.4.2",
    }, headers=API_KEY_HEADER)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert "operation_id" in data


def test_teardown_endpoint():
    db = SessionLocal()
    node = Node(name="teardown-node", uuid="uuid-td", host="http://localhost:8009")
    db.add(node)
    db.commit()
    db.refresh(node)
    db.add(ServiceInstance(
        node_id=node.id,
        name="api-gateway",
        service_uuid="api-gateway",
        flake="github:org/repo#api",
        commands={"teardown": "api"},
    ))
    db.commit()
    db.close()

    r = client.post("/teardown", json={
        "service": "api-gateway",
        "confirm": True,
    }, headers=API_KEY_HEADER)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"


def test_rollback_endpoint():
    r = client.post("/rollback", json={
        "service": "api-gateway",
        "target_version": "v1.4.1",
    }, headers=API_KEY_HEADER)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"


def test_operation_status():
    # Setup node
    db = SessionLocal()
    node = Node(name="status-node", uuid="uuid-2", host="http://localhost:8002")
    db.add(node)
    db.commit()
    db.refresh(node)
    db.add(ServiceInstance(
        node_id=node.id,
        name="api-gateway",
        service_uuid="api-gateway",
        flake="github:org/repo#api",
        commands={"deploy": "api"},
    ))
    db.commit()
    db.close()

    # Create a deploy first
    r = client.post("/deploy", json={
        "service": "api-gateway",
        "node_name": "status-node",
        "version": "v1.0.0",
    }, headers=API_KEY_HEADER)
    op_id = r.json()["operation_id"]

    # Check its status
    r = client.get(f"/operations/{op_id}", headers=API_KEY_HEADER)
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == op_id
    assert data["type"] == "deploy"
    assert data["service"] == "api-gateway"


def test_list_operations():
    # Setup node
    db = SessionLocal()
    node = Node(name="list-node", uuid="uuid-3", host="http://localhost:8003")
    db.add(node)
    db.commit()
    db.refresh(node)
    db.add(ServiceInstance(
        node_id=node.id,
        name="svc1",
        service_uuid="svc1",
        flake="github:org/repo#svc1",
        commands={"deploy": "svc1"},
    ))
    db.add(ServiceInstance(
        node_id=node.id,
        name="svc2",
        service_uuid="svc2",
        flake="github:org/repo#svc2",
        commands={"deploy": "svc2"},
    ))
    db.commit()
    db.close()

    # Create a couple operations
    client.post("/deploy", json={"service": "svc1", "node_name": "list-node", "version": "v1"}, headers=API_KEY_HEADER)
    client.post("/deploy", json={"service": "svc2", "node_name": "list-node", "version": "v2"}, headers=API_KEY_HEADER)

    r = client.get("/operations", headers=API_KEY_HEADER)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert len(data["operations"]) == 2


def test_operation_not_found():
    r = client.get("/operations/nonexistent", headers=API_KEY_HEADER)
    assert r.status_code == 404


def test_deploy_unauthorized():
    r = client.post("/deploy", json={
        "service": "api-gateway",
        "node_name": "auth-node",
        "version": "v1",
    })
    assert r.status_code == 401


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_nodes_endpoint():
    db = SessionLocal()
    db.add(Node(name="n1", uuid="u1", host="http://localhost:8001"))
    db.commit()
    db.close()

    r = client.get("/nodes", headers=API_KEY_HEADER)
    assert r.status_code == 200
    assert any(n["name"] == "n1" for n in r.json())

def test_command_deploy_success():
    db = SessionLocal()
    node = Node(name = "cmd-node", uuid="cmd-uuid", host = "http://localhost:8001")
    db.add(node)
    db.commit()
    db.refresh(node)
    db.add(ServiceInstance(
        node_id = node.id,
        name = "cmd-svc",
        service_uuid = "cmd-svc",
        flake = "github:org/repo#app",
        commands = {"deploy":"default"},
    ))
    db.commit()
    db.close()

    r = client.post("/command", json={
        "service":"cmd-svc",
        "command":"deploy",
    },headers = API_KEY_HEADER)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert "operation_id" in data

    db = SessionLocal()
    op = db.query(Operation).filter_by(id=data["operation_id"]).first()
    assert op is not None
    assert op.type == "command"
    assert op.service_name =="cmd-svc"
    assert op.metadata_json["command"] == "deploy"
    assert op.metadata_json["command_flake"] is not None
    db.close()

def test_command_missing_in_manifest():
    db = SessionLocal()
    node = Node(name = "cmd-node2", uuid = "cmd-uuid2", host = "http://localhost:8001")
    db.add(node)
    db.commit()
    db.refresh(node)
    db.add(ServiceInstance(
        node_id=node.id,
        name="cmd-svc2",
        service_uuid="cmd-svc2",
        flake="github:org/repo#app",
        commands={"deploy": "default"}, 
    ))
    db.commit()
    db.close()

    r = client.post("/command", json={
        "service": "cmd-svc2",
        "command": "ci",
    }, headers=API_KEY_HEADER)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "failed"
    assert "ci" in data["message"]

    db = SessionLocal()
    op = db.query(Operation).filter_by(id=data["operation_id"]).first()
    assert op is not None
    assert op.status == "failed"
    assert op.finished_at is not None
    db.close()

def test_teardown_uses_command_path():
    db = SessionLocal()
    node = Node(name="td-cmd-node", uuid="td-cmd-uuid", host="http://localhost:8009")
    db.add(node)
    db.commit()
    db.refresh(node)
    db.add(ServiceInstance(
        node_id=node.id,
        name="td-cmd-svc",
        service_uuid="td-cmd-svc",
        flake="github:org/repo#app",
        commands={"teardown": "stop"},
    ))
    db.commit()
    db.close()
    r = client.post("/teardown", json={
        "service": "td-cmd-svc",
    }, headers=API_KEY_HEADER)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert "operation_id" in data
    
    db = SessionLocal()
    op = db.query(Operation).filter_by(id=data["operation_id"]).first()
    assert op is not None
    assert op.type == "teardown"
    assert op.metadata_json["command"] == "teardown"
    assert op.metadata_json["command_flake"] is not None
    db.close()


