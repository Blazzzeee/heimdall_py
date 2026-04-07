import os
import hashlib
import hmac
import json
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("HEIMDALL_ENV", "test")
os.environ.setdefault("HEIMDALL_ALLOW_DEFAULTS", "1")
os.environ.setdefault("INFRA_API_KEY", "heimdall")
os.environ.setdefault("WEBHOOK_SECRET", "super-secret-key")

from api import app, WEBHOOK_SECRET
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

client = TestClient(app)

def sign_payload(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return body, {"X-Signature": f"sha256={digest}", "Content-Type": "application/json"}

def test_webhook_deploy_success():
    # Setup node
    db = SessionLocal()
    node = Node(name="test-node", uuid="test-node-uuid", host="localhost", env="prod")
    db.add(node)
    db.commit()
    db.close()

    payload = {
        "action": "deploy",
        "service": "api",
        "version": "v1",
        "env": "prod"
    }
    body, headers = sign_payload(payload)
    
    response = client.post('/webhook', data=body, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}

    # Verify Operation created
    db = SessionLocal()
    ops = db.query(Operation).all()
    assert len(ops) == 1
    assert ops[0].type == "deploy"
    db.close()

def test_webhook_register_success():
    payload = {
        "action": "register",
        "service": "node-v2",
        "env": "prod",
        "metadata": {
            "name": "node-v2",
            "host": "http://localhost:8002"
        }
    }
    body, headers = sign_payload(payload)
    
    response = client.post('/webhook', data=body, headers=headers)
    assert response.status_code == 200
    
    db = SessionLocal()
    node = db.query(Node).filter(Node.name == "node-v2").first()
    assert node is not None
    assert node.host == "http://localhost:8002"
    db.close()

def test_webhook_invalid_signature():
    payload = {"action": "deploy", "service": "api", "env": "prod"}
    body, headers = sign_payload(payload)
    headers["X-Signature"] = "sha256=invalid"
    
    response = client.post('/webhook', data=body, headers=headers)
    assert response.status_code == 401

def test_nodes_endpoint():
    # Register a node first
    test_webhook_register_success()
    
    response = client.get('/nodes', headers={"X-API-Key": "heimdall"})
    assert response.status_code == 200
    nodes = response.json()
    assert any(n['name'] == 'node-v2' for n in nodes)
