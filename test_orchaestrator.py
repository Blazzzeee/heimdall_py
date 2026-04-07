import os
import asyncio
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("HEIMDALL_ALLOW_DEFAULTS", "1")
os.environ.setdefault("INFRA_API_KEY", "heimdall")
os.environ.setdefault("WEBHOOK_SECRET", "super-secret-key")

import api
from datetime import datetime, UTC
from db import SessionLocal, Node, Operation, ServiceInstance, init_db

class DummyResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def json(self):
        return {
            "status": "alive",
            "time": datetime.now(UTC).isoformat() + "Z"
        }

class FakeSuccessClient:
    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        return DummyResponse(200)

class FakeFailClient:
    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        raise Exception("node unreachable")

@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    db = SessionLocal()
    db.query(Operation).delete()
    db.query(ServiceInstance).delete()
    db.query(Node).delete()
    db.commit()
    db.close()

def test_node_heartbeat():
    # Note: node.app was the separate node.py app. 
    # Since we merged orchestrator into api, we still have node.py as a separate file.
    import node
    client = TestClient(node.app)
    r = client.get("/heartbeat")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "alive"

def test_orchestrator_check_node_online(monkeypatch):
    db = SessionLocal()
    test_node = Node(name="node1", uuid="node1-uuid", host="http://localhost:8001", status="UNKNOWN", fail_count=1)
    db.add(test_node)
    db.commit()
    node_id = test_node.id
    db.close()

    monkeypatch.setattr(api.httpx, "AsyncClient", FakeSuccessClient)

    asyncio.run(api.check_node(node_id))

    db = SessionLocal()
    updated_node = db.query(Node).filter(Node.id == node_id).first()
    assert updated_node.status == "ONLINE"
    assert updated_node.fail_count == 0
    assert updated_node.last_seen is not None
    db.close()

def test_orchestrator_check_node_offline(monkeypatch):
    db = SessionLocal()
    test_node = Node(name="node1", uuid="node1-uuid", host="http://localhost:8001", status="UNKNOWN", fail_count=api.FAIL_THRESHOLD - 1)
    db.add(test_node)
    db.commit()
    node_id = test_node.id
    db.close()

    monkeypatch.setattr(api.httpx, "AsyncClient", FakeFailClient)

    asyncio.run(api.check_node(node_id))

    db = SessionLocal()
    updated_node = db.query(Node).filter(Node.id == node_id).first()
    assert updated_node.status == "OFFLINE"
    assert updated_node.fail_count == api.FAIL_THRESHOLD
    db.close()

def test_orchestrator_node_status_transition(monkeypatch):
    db = SessionLocal()
    test_node = Node(name="node1", uuid="node1-uuid", host="http://localhost:8001", status="UNKNOWN", fail_count=0)
    db.add(test_node)
    db.commit()
    node_id = test_node.id
    db.close()

    monkeypatch.setattr(api.httpx, "AsyncClient", FakeSuccessClient)
    asyncio.run(api.check_node(node_id))
    
    db = SessionLocal()
    assert db.query(Node).filter(Node.id == node_id).first().status == "ONLINE"
    db.close()

    monkeypatch.setattr(api.httpx, "AsyncClient", FakeFailClient)
    for _ in range(api.FAIL_THRESHOLD):
        asyncio.run(api.check_node(node_id))
    
    db = SessionLocal()
    updated_node = db.query(Node).filter(Node.id == node_id).first()
    assert updated_node.status == "OFFLINE"
    assert updated_node.fail_count >= api.FAIL_THRESHOLD
    db.close()

def test_orchestrator_nodes_endpoint(monkeypatch):
    # Register a node
    db = SessionLocal()
    test_node = Node(name="node1", uuid="node1-uuid", host="http://localhost:8001")
    db.add(test_node)
    db.commit()
    db.close()

    # Prevent monitor task racing in test client startup
    monkeypatch.setattr(api.app.router, "on_startup", [])
    client = TestClient(api.app)
    r = client.get("/nodes", headers={"X-API-Key": api.INFRA_API_KEY})
    assert r.status_code == 200
    body = r.json()
    assert any(n["name"] == "node1" for n in body)
