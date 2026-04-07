"""
Operation runners — DB-backed, integrating with fastapi_agent.
"""

import asyncio
import httpx
import hmac
import hashlib
import json
import time
from datetime import datetime, UTC
from db import SessionLocal, Operation
from app.models import DeployRequest, TeardownRequest, RollbackRequest
from app.config import WEBHOOK_SECRET

WEBHOOK_SECRET_BYTES = WEBHOOK_SECRET.encode()

def _mark(op_id: str, status: str, message: str, error: str = None):
    db = SessionLocal()
    try:
        op = db.query(Operation).filter(Operation.id == op_id).first()
        if op:
            op.status = status
            op.message = message
            op.error = error
            if status in ("success", "failed"):
                op.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()

def _generate_hmac_signature(body_str: str, timestamp: str) -> str:
    message = body_str + timestamp
    return hmac.new(WEBHOOK_SECRET_BYTES, message.encode(), hashlib.sha256).hexdigest()

async def send_agent_command(node_host: str, payload: dict):
    """Send an HMAC-signed POST to the remote agent's /command endpoint."""
    body_str = json.dumps(payload)
    timestamp = str(int(time.time()))
    signature = _generate_hmac_signature(body_str, timestamp)

    headers = {
        "X-Timestamp": timestamp,
        "X-Signature": signature,
        "Content-Type": "application/json"
    }
    
    # Strip trailing slash if present on node_host
    base_url = node_host.rstrip('/')
    url = f"{base_url}/command"

    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(url, content=body_str.encode(), headers=headers)
        res.raise_for_status()
        return res.json()
async def send_agent_inspect(node_host: str, flake_path: str):
    """Ask agent to inspect a flake and return metadata."""
    payload = {"flake": flake_path}
    body_str = json.dumps(payload)
    
    # We'll use the same HMAC signing for security
    timestamp = str(int(time.time()))
    signature = _generate_hmac_signature(body_str, timestamp)

    headers = {
        "X-Timestamp": timestamp,
        "X-Signature": signature,
        "Content-Type": "application/json"
    }
    
    base_url = node_host.rstrip('/')
    url = f"{base_url}/inspect"

    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(url, content=body_str.encode(), headers=headers)
        res.raise_for_status()
        return res.json()

async def run_deploy(op_id: str, req: DeployRequest, node_host: str | None = None):
    _mark(op_id, "running", f"Sending deploy command to agent on {req.node_name}...")
    try:
        if not node_host:
            raise ValueError("Node host is required to dispatch deploy to agent.")
        if not req.flake:
            raise ValueError("A Nix flake reference is required to hit the agent's /command endpoint.")
        
        payload = {
            "operation_id": op_id,
            "service": req.service,
            "flake": req.flake,
            "healthcheck_url": req.healthcheck_url,
        }
        
        response = await send_agent_command(node_host, payload)
        
        if response.get("status") == "accepted":
            _mark(op_id, "running", f"Agent accepted deployment. Waiting for health status...")
            # Note: The actual success/fail status will be pushed asynchronously 
            # by the agent via the /webhook endpoint. We leave the operation as "running".
        elif response.get("status") == "already running":
            _mark(op_id, "running", f"Service is currently locked/deploying. Waiting...")
        else:
            err = response.get("error", "Unknown error")
            _mark(op_id, "failed", f"Agent rejected deployment: {err}", error=err)

    except Exception as e:
        _mark(op_id, "failed", "Failed to communicate with agent.", error=str(e))


async def run_teardown(op_id: str, req: TeardownRequest):
    _mark(op_id, "running", f"Tearing down {req.service}...")
    try:
        # Agent doesn't explicitly support teardown yet via /command, 
        # so this remains a simulated block.
        await asyncio.sleep(2)
        _mark(op_id, "success", f"Torn down {req.service}.")
    except Exception as e:
        _mark(op_id, "failed", "Teardown failed.", error=str(e))


async def run_rollback(op_id: str, req: RollbackRequest):
    _mark(op_id, "running", f"Rolling back {req.service} → {req.target_version}...")
    try:
        # Agent has no specific rollback endpoint yet.
        await asyncio.sleep(2)
        _mark(op_id, "success", f"Rolled back {req.service} to {req.target_version} in {req.environment}.")
    except Exception as e:
        _mark(op_id, "failed", "Rollback failed.", error=str(e))
