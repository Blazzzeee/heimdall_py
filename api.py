"""
Heimdall — Unified Infra Control API

Endpoints:
  POST /webhook          — HMAC-signed webhook ingestion (deploy/register)
  POST /deploy           — Deploy a service (API key auth)
  POST /command          — Run a manifest command (API key auth)
  POST /teardown         — Teardown a service (API key auth)
  POST /rollback         — Rollback a service (API key auth)
  GET  /operations/{id}  — Check operation status (API key auth)
  GET  /operations       — List recent operations (API key auth)
  GET  /nodes            — List registered nodes
  GET  /health           — Health check
"""

import hashlib
import hmac
import uuid
import json
import asyncio
import httpx
import time
from datetime import datetime, UTC
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from threading import Lock

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

load_dotenv()

from db import SessionLocal, Node, ServiceInstance, Operation, init_db
from app.models import (
    DeployRequest, DeployResponse,
    CommandRequest, CommandResponse,
    TeardownRequest, TeardownResponse,
    RollbackRequest, RollbackResponse,
    OperationStatus,
    DeclareServiceRequest,
    DeployAllResponse,
    RegisterNodeRequest, RegisterNodeResponse,
)
from app.auth import verify_api_key
from app.config import (
    INFRA_API_KEY,
    WEBHOOK_TTL_SECONDS,
    FAIL_THRESHOLD,
    MONITOR_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS,
)
from app.logging_utils import setup_logging, bind_request_id
from app.ops import run_deploy, run_rollback, run_command, send_agent_inspect


# ── App setup ─────────────────────────────────────────────────────────────────

logger = setup_logging("heimdall.api")
_op_update_lock = Lock()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Heimdall starting up...")
    monitor_task = asyncio.create_task(monitor())
    yield
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass

app = FastAPI(
    title="Heimdall — Infra Control API",
    description="Deterministic infrastructure control — deploy, teardown, rollback, monitor.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    bind_request_id(request_id)
    start = time.time()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response

init_db()


# ── Models ────────────────────────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    action: str
    service: str
    version: str | None = None
    metadata: dict | None = None


# ── Webhook helpers ───────────────────────────────────────────────────────────

def get_all_nodes():
    db = SessionLocal()
    try:
        return db.query(Node).all()
    finally:
        db.close()

def resolve_command_flake(svc: ServiceInstance, command_name: str) -> tuple[str | None, str | None, str | None]:
    if not svc or not svc.flake:
        return None, None, "Service has no flake configured."
    commands = svc.commands or {}
    if not isinstance(commands, dict):
        return None, None, "Service manifest commands are invalid or missing."
    if command_name not in commands:
        return None, None, f"Command '{command_name}' is not supported by the manifest."
    command_value = commands[command_name]
    if not isinstance(command_value, str) or not command_value.strip():
        return None, None, f"Command '{command_name}' is invalid in the manifest."
    command_value = command_value.strip()
    base_ref = svc.flake.split("#")[0]
    if any(sep in command_value for sep in (":", "/", "#")):
        return command_value, "flake", None
    return f"{base_ref}#{command_value}", "flake", None


def get_or_create_service(node: Node, payload: WebhookPayload):
    db = SessionLocal()
    try:
        service = db.query(ServiceInstance).filter(
            ServiceInstance.node_id == node.id,
            ServiceInstance.name == payload.service,
        ).first()

        if not service:
            service = ServiceInstance(
                node_id=node.id,
                name=payload.service,
                service_uuid=payload.service,
                flake="default",
                commands=[],
            )
            db.add(service)
            db.commit()
            db.refresh(service)
        return service
    finally:
        db.close()


def create_operation(service_id: str, op_type: str, metadata: dict):
    db = SessionLocal()
    try:
        operation = Operation(
            service_id=service_id,
            type=op_type,
            metadata_json=metadata,
        )
        db.add(operation)
        db.commit()
        db.refresh(operation)
        return operation
    finally:
        db.close()


def handle_webhook(payload: WebhookPayload):
    if payload.action == "register":
        db = SessionLocal()
        try:
            name = payload.metadata.get("name") if payload.metadata else payload.service
            host = payload.metadata.get("host") if payload.metadata else "http://localhost:8000"

            node = db.query(Node).filter(Node.name == name).first()
            if not node:
                node = Node(
                    name=name,
                    uuid=name,
                    host=host,
                )
                db.add(node)
                db.commit()
        finally:
            db.close()
        return

    nodes = get_all_nodes()
    for node in nodes:
        service = get_or_create_service(node, payload)
        create_operation(
            service_id=service.id,
            op_type=payload.action,
            metadata={"version": payload.version},
        )


# ── Node monitoring ──────────────────────────────────────────────────────────

async def check_node(node_id: str):
    db = SessionLocal()
    try:
        node = db.query(Node).filter(Node.id == node_id).first()
        if not node:
            return
        prev_status = node.status

        try:
            async with httpx.AsyncClient(timeout=HEARTBEAT_TIMEOUT_SECONDS) as client:
                res = await client.get(f"{node.host}/heartbeat")
                if res.status_code == 200:
                    node.status = "ONLINE"
                    node.fail_count = 0
                    node.last_seen = datetime.now(UTC).isoformat() + "Z"
                    db.commit()
                    return
        except Exception:
            pass

        node.fail_count += 1
        if node.fail_count >= FAIL_THRESHOLD:
            node.status = "OFFLINE"
            # If the node is unreachable, any service status from the agent is stale.
            # Mark services as dead so UIs (Discord, etc.) stop showing "healthy".
            if prev_status != "OFFLINE":
                db.query(ServiceInstance).filter(ServiceInstance.node_id == node.id).update(
                    {ServiceInstance.status: "dead"},
                    synchronize_session=False,
                )
        db.commit()
    finally:
        db.close()


async def monitor():
    while True:
        db = SessionLocal()
        try:
            node_ids = [n.id for n in db.query(Node).all()]
        finally:
            db.close()

        tasks = [check_node(nid) for nid in node_ids]
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


# ── Webhook auth ─────────────────────────────────────────────────────────────

def verify_webhook_signature(payload: bytes, x_signature: str, x_timestamp: str | None = None):
    if x_timestamp:
        # Agent style: hash(body + timestamp)
        try:
            ts = int(x_timestamp)
            if abs(time.time() - ts) > WEBHOOK_TTL_SECONDS:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook timestamp expired")
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook timestamp")
        message = payload.decode('utf-8') + x_timestamp
        expected = hmac.new(INFRA_API_KEY.encode("utf-8"), message.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_signature):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent webhook signature")
    else:
        # Legacy style
        if not x_signature.startswith("sha256="):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature header")
        expected = x_signature.split("=", 1)[1]
        mac = hmac.new(INFRA_API_KEY.encode("utf-8"), payload, hashlib.sha256)
        computed = mac.hexdigest()
        if not hmac.compare_digest(computed, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid legacy webhook signature")


# ── Webhook Handlers ────────────────────────────────────────────────────────

def handle_legacy_webhook(payload: dict):
    from pydantic import BaseModel
    class LegacyWebhookPayload(BaseModel):
        action: str
        service: str
        version: str | None = None
        metadata: dict | None = None
        
    p = LegacyWebhookPayload(**payload)
    if p.action == "register":
        db = SessionLocal()
        try:
            name = p.metadata.get("name") if p.metadata else p.service
            host = p.metadata.get("host") if p.metadata else "http://localhost:8000"

            node = db.query(Node).filter(Node.name == name).first()
            if not node:
                node = Node(
                    name=name,
                    uuid=name,
                    host=host,
                )
                db.add(node)
                db.commit()
        finally:
            db.close()
        return

    nodes = get_all_nodes()
    for node in nodes:
        service = get_or_create_service(node, p)
        create_operation(
            service_id=service.id,
            op_type=p.action,
            metadata={"version": p.version},
        )


def handle_agent_webhook(payload: dict):
    wtype = payload.get("type")
    
    if wtype == "node_status":
        # Example: {"type": "node_status", "services": {"api": {"pid": 123, "status": "healthy"}}}
        services = payload.get("services", {})
        if services:
            db = SessionLocal()
            try:
                for svc_name, data in services.items():
                    svc = db.query(ServiceInstance).filter(ServiceInstance.name == svc_name).first()
                    if svc and data.get("status"):
                        svc.status = data["status"]
                db.commit()
            finally:
                db.close()
        
    elif wtype == "status":
        # Example: {"type": "status", "service": "api", "status": "healthy|dead|failed", "error": "..."}
        svc_name = payload.get("service")
        new_status = payload.get("status")
        err = payload.get("error")
        with _op_update_lock:
            db = SessionLocal()
            try:
                svc = db.query(ServiceInstance).filter(ServiceInstance.name == svc_name).first()
                svc_id = svc.id if svc else None

                # Find the most recent 'running' operation for this service
                query = db.query(Operation).filter(Operation.status == "running")
                if svc_id:
                    query = query.filter(Operation.service_id == svc_id)
                else:
                    query = query.filter(Operation.service_name == svc_name)

                op = query.order_by(Operation.created_at.desc()).first()

                if op:
                    if new_status == "healthy":
                        op.status = "success"
                        op.message = "Agent reported service is healthy."
                        op.finished_at = datetime.now(UTC)
                    elif new_status == "stopped":
                        op.status = "success"
                        op.message = "Agent reported service exited cleanly."
                        op.finished_at = datetime.now(UTC)
                    elif new_status in ("failed", "dead"):
                        op.status = "failed"
                        op.message = f"Agent reported service is {new_status}."
                        op.error = err
                        op.finished_at = datetime.now(UTC)
                    db.commit()

                # Also update ServiceInstance if you wish
                svc = db.query(ServiceInstance).filter(ServiceInstance.name == svc_name).first()
                if svc:
                    svc.status = new_status
                    db.commit()
            finally:
                db.close()
            
    elif wtype == "logs_batch":
        # Example: {"type": "logs_batch", "logs": [{"service": "api", "stream": "stdout", "log": "..."}]}
        logs = payload.get("logs", [])
        for log_entry in logs:
            svc = log_entry.get("service", "unknown")
            stream = log_entry.get("stream", "output")
            log_line = log_entry.get("log", "")
            # Print remotely streamed logs to the control plane stdout for now
            print(f"[Remote {svc} | {stream}] {log_line}")

def handle_webhook(payload: dict):
    if "action" in payload:
        handle_legacy_webhook(payload)
    elif "type" in payload:
        handle_agent_webhook(payload)

# ── Endpoints: Health ────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "timestamp": time.time()}


# ── Endpoints: Webhook ───────────────────────────────────────────────────────

@app.post("/webhook", tags=["webhook"])
async def webhook_listener(
    request: Request, 
    x_signature: str = Header(...),
    x_timestamp: str = Header(None)
):
    body = await request.body()
    verify_webhook_signature(body, x_signature, x_timestamp)

    payload = json.loads(body)
    handle_webhook(payload)

    return {"status": "accepted"}


# ── Endpoints: Services (Declarative) ────────────────────────────────────────

@app.post("/services", tags=["services"])
async def declare_service(
    req: DeclareServiceRequest,
    _: str = Depends(verify_api_key),
):
    db = SessionLocal()
    try:
        node = db.query(Node).filter(Node.name == req.node_name).first()
        if not node:
            raise HTTPException(status_code=404, detail=f"Node '{req.node_name}' not found")
        node_host = node.host
        node_host = node.host
            
        svc = db.query(ServiceInstance).filter(
            ServiceInstance.node_id == node.id,
            ServiceInstance.name == req.service
        ).first()
        
        if not svc:
            svc = ServiceInstance(
                node_id=node.id,
                name=req.service,
                service_uuid=req.service,
            )
            db.add(svc)
            
        # Discover metadata from manifest when available
        manifest_commands = None
        manifest_healthcheck = None
        if req.flake:
            try:
                inspection = await send_agent_inspect(node.host, req.flake)
                if inspection.get("status") == "success":
                    manifest = inspection.get("manifest", {})
                    manifest_commands = manifest.get("commands")
                    manifest_healthcheck = manifest.get("healthcheck_url")
            except Exception as e:
                print(f"Manifest inspection failed: {e}")

        svc.repo_url = req.repo_url
        svc.flake = req.flake
        if isinstance(manifest_commands, dict):
            svc.commands = manifest_commands
        if manifest_healthcheck is not None:
            svc.healthcheck_url = manifest_healthcheck
        svc.triggered_by = req.triggered_by  # Audit
        
        db.commit()
        return {"status": "success", "message": f"Service '{req.service}' declared on node '{req.node_name}'."}
    finally:
        db.close()


@app.get("/services", tags=["services"])
async def list_services(
    _: str = Depends(verify_api_key),
):
    db = SessionLocal()
    try:
        services = db.query(ServiceInstance).all()
        return [
            {
                "name": s.name,
                "status": s.status,
                "node_name": s.node.name,
                "triggered_by": s.triggered_by,
                "healthcheck_url": s.healthcheck_url,
            }
            for s in services
        ]
    finally:
        db.close()


@app.get("/services/{service_name}", tags=["services"])
async def get_service_detail(
    service_name: str,
    _: str = Depends(verify_api_key),
):
    db = SessionLocal()
    try:
        svc = db.query(ServiceInstance).filter(ServiceInstance.name == service_name).first()
        if not svc:
            raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found")
        
        return {
            "name": svc.name,
            "status": svc.status,
            "healthcheck_url": svc.healthcheck_url,
            "node": svc.node.name,
            "node_host": svc.node.host,
            "repo_url": svc.repo_url,
            "flake": svc.flake,
            "triggered_by": svc.triggered_by,
            "created_at": svc.created_at.isoformat() if svc.created_at else None,
        }
    finally:
        db.close()

# ── Endpoints: Deploy ────────────────────────────────────────────────────────

@app.post("/deploy-all", response_model=DeployAllResponse, tags=["operations"])
async def deploy_all(
    triggered_by: str = None,
    _: str = Depends(verify_api_key),
):
    db = SessionLocal()
    try:
        services = db.query(ServiceInstance).all()
        if not services:
            return DeployAllResponse(status="success", message="No services to deploy.", operation_ids=[])
        
        op_ids = []
        for svc in services:
            healthcheck_url = None
            if svc.flake:
                try:
                    inspection = await send_agent_inspect(svc.node.host, svc.flake)
                    if inspection.get("status") == "success":
                        manifest = inspection.get("manifest", {})
                        manifest_healthcheck = manifest.get("healthcheck_url")
                        if manifest_healthcheck is not None:
                            healthcheck_url = manifest_healthcheck
                except Exception as e:
                    print(f"Manifest inspection failed during deploy-all: {e}")

            command_value, command_kind, command_error = resolve_command_flake(svc, "deploy")
            op_id = str(uuid.uuid4())
            op = Operation(
                id=op_id,
                type="deploy",
                service_id=svc.id,
                service_name=svc.name,
                version="latest",
                triggered_by=triggered_by,
                status="failed" if command_error else "pending",
                message=command_error or "Queued (Bulk)",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC) if command_error else None,
                metadata_json={
                    "repo_url": svc.repo_url,
                    "flake": svc.flake,
                    "commands": svc.commands,
                    "command": "deploy",
                    "command_flake": command_value if command_kind == "flake" else None,
                    "healthcheck_url": healthcheck_url,
                }
            )
            db.add(op)
            db.commit()

            if command_error:
                op_ids.append(op_id)
                continue

            # Use real node host
            node_host = svc.node.host
            # Create a dummy request for the background task
            req = DeployRequest(
                service=svc.name,
                version="latest",
                commands=svc.commands,
                triggered_by=triggered_by
            )
            asyncio.create_task(run_command(
                op_id,
                req,
                node_host,
                healthcheck_url,
                command_value,
                "deploy",
                command_kind,
            ))
            op_ids.append(op_id)
            
        return DeployAllResponse(status="success", message=f"Queued {len(op_ids)} deployments.", operation_ids=op_ids)
    finally:
        db.close()

@app.post("/deploy", response_model=DeployResponse, tags=["operations"])
async def deploy(
    req: DeployRequest,
    _: str = Depends(verify_api_key),
):
    db = SessionLocal()
    try:
        # Try to infer from a declared service
        svc = db.query(ServiceInstance).filter(ServiceInstance.name == req.service).first()
        if not req.node_name:
            if not svc:
                raise HTTPException(status_code=400, detail=f"Service '{req.service}' not declared. Please provide node_name.")
            req.node_name = svc.node.name
        if svc:
            req.commands = req.commands or svc.commands

        # Resolve node
        node = db.query(Node).filter(Node.name == req.node_name).first()
        if not node:
            raise HTTPException(status_code=404, detail=f"Node '{req.node_name}' not found")
        
        flake = svc.flake if svc else None
        if not flake:
            raise HTTPException(status_code=400, detail=f"Service '{req.service}' has no flake configured. Declare the service with a flake first.")

        healthcheck_url = None
        if flake:
            try:
                inspection = await send_agent_inspect(node.host, flake)
                if inspection.get("status") == "success":
                    manifest = inspection.get("manifest", {})
                    manifest_healthcheck = manifest.get("healthcheck_url")
                    if manifest_healthcheck is not None:
                        healthcheck_url = manifest_healthcheck
            except Exception as e:
                print(f"Manifest inspection failed during deploy: {e}")

        command_value, command_kind, command_error = resolve_command_flake(svc, "deploy")
        
        node_host = node.host

        # Link to service if found
        svc_id = svc.id if svc else None

        # Clean up stale operations for this service
        state_ops = db.query(Operation).filter(
            Operation.service_name == req.service,
            Operation.status.in_(["pending", "running"])
        ).all()
        for old_op in state_ops:
            old_op.status = "failed"
            old_op.message = "Superseded by new deployment."
            old_op.finished_at = datetime.now(UTC)

        op_id = str(uuid.uuid4())
        op = Operation(
            id=op_id,
            type="deploy",
            service_id=svc_id,
            service_name=req.service,
            version=req.version,
            triggered_by=req.triggered_by,
            status="failed" if command_error else "pending",
            message=command_error or "Queued",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC) if command_error else None,
            metadata_json={
                "repo_url": svc.repo_url if svc else None,
                "flake": flake,
                "commands": svc.commands if svc else None,
                "command": "deploy",
                "command_flake": command_value if command_kind == "flake" else None,
                "healthcheck_url": healthcheck_url,
            }
        )
        db.add(op)
        db.commit()
    finally:
        db.close()

    if command_error:
        return DeployResponse(
            operation_id=op_id,
            status="failed",
            message=command_error,
        )

    asyncio.create_task(run_command(
        op_id,
        req,
        node_host,
        healthcheck_url,
        command_value,
        "deploy",
        command_kind,
    ))
    return DeployResponse(
        operation_id=op_id,
        status="pending",
        message=f"Deploy of {req.service}@{req.version} to {req.node_name} queued.",
    )


# ── Endpoints: Command ───────────────────────────────────────────────────────

@app.post("/command", response_model=CommandResponse, tags=["operations"])
async def command(
    req: CommandRequest,
    _: str = Depends(verify_api_key),
):
    op_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        svc = db.query(ServiceInstance).filter(ServiceInstance.name == req.service).first()
        if not svc:
            raise HTTPException(status_code=400, detail=f"Service '{req.service}' not declared.")
        if not req.node_name:
            req.node_name = svc.node.name

        node = db.query(Node).filter(Node.name == req.node_name).first()
        if not node:
            raise HTTPException(status_code=404, detail=f"Node '{req.node_name}' not found")

        command_value, command_kind, command_error = resolve_command_flake(svc, req.command)
        logger.info(
        "resolved_command_flake",
        extra={
           "service": req.service,
           "command": req.command,
           "command_flake": command_value,
           "command_kind": command_kind,
           "error": command_error,
            },
        )
        node_host = node.host

        op = Operation(
            id=op_id,
            type="command",
            service_id=svc.id,
            service_name=req.service,
            triggered_by=req.triggered_by,
            status="failed" if command_error else "pending",
            message=command_error or "Queued",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC) if command_error else None,
            metadata_json={
                "repo_url": svc.repo_url,
                "flake": svc.flake,
                "commands": svc.commands,
                "command": req.command,
                "command_flake": command_value if command_kind == "flake" else None,
            }
        )
        db.add(op)
        db.commit()
    finally:
        db.close()

    if command_error:
        return CommandResponse(
            operation_id=op_id,
            status="failed",
            message=command_error,
        )

    asyncio.create_task(run_command(
        op_id,
        DeployRequest(service=req.service, node_name=req.node_name),
        node_host,
        None,
        command_value,
        req.command,
        command_kind,
    ))
    return CommandResponse(
        operation_id=op_id,
        status="pending",
        message=f"Command '{req.command}' queued for {req.service}.",
    )

# ── Endpoints: Teardown ─────────────────────────────────────────────────────

@app.post("/teardown", response_model=TeardownResponse, tags=["operations"])
async def teardown(
    req: TeardownRequest,
    _: str = Depends(verify_api_key),
):
    op_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        svc = db.query(ServiceInstance).filter(ServiceInstance.name == req.service).first()
        if not svc:
            raise HTTPException(status_code=400, detail=f"Service '{req.service}' not declared.")

        command_value, command_kind, command_error = resolve_command_flake(svc, "teardown")
        node_host = svc.node.host
        node_name = svc.node.name

        op = Operation(
            id=op_id,
            type="teardown",
            service_id=svc.id if svc else None,
            service_name=req.service,
            triggered_by=req.triggered_by,
            status="failed" if command_error else "pending",
            message=command_error or "Queued",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC) if command_error else None,
            metadata_json={
                "repo_url": svc.repo_url if svc else None,
                "flake": svc.flake if svc else None,
                "commands": svc.commands if svc else None,
                "command": "teardown",
                "command_flake": command_value if command_kind == "flake" else None,
                "command_exec": command_value if command_kind == "exec" else None,
            }
        )
        db.add(op)
        db.commit()
    finally:
        db.close()

    if command_error:
        return TeardownResponse(
            operation_id=op_id,
            status="failed",
            message=command_error,
        )

    asyncio.create_task(run_command(
        op_id,
        DeployRequest(service=req.service, node_name=node_name),
        node_host,
        None,
        command_value,
        "teardown",
        command_kind,
    ))
    return TeardownResponse(
        operation_id=op_id,
        status="pending",
        message=f"Teardown of {req.service} queued.",
    )


# ── Endpoints: Rollback ─────────────────────────────────────────────────────

@app.post("/rollback", response_model=RollbackResponse, tags=["operations"])
async def rollback(
    req: RollbackRequest,
    _: str = Depends(verify_api_key),
):
    op_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        svc = db.query(ServiceInstance).filter(ServiceInstance.name == req.service).first()
        op = Operation(
            id=op_id,
            type="rollback",
            service_id=svc.id if svc else None,
            service_name=req.service,
            target_version=req.target_version,
            triggered_by=req.triggered_by,
            message="Queued",
            started_at=datetime.now(UTC),
        )
        db.add(op)
        db.commit()
    finally:
        db.close()

    asyncio.create_task(run_rollback(op_id, req))
    return RollbackResponse(
        operation_id=op_id,
        status="pending",
        message=f"Rollback of {req.service} to {req.target_version} queued.",
    )


@app.get("/operations/audit", tags=["operations"])
async def get_audit_logs(
    limit: int = 50,
    _: str = Depends(verify_api_key),
):
    db = SessionLocal()
    try:
        ops = db.query(Operation).order_by(Operation.created_at.desc()).limit(limit).all()
        return [
            {
                "id": op.id,
                "type": op.type,
                "service": op.service_name,
                "status": op.status,
                "triggered_by": op.triggered_by,
                "created_at": op.created_at.isoformat() if op.created_at else None,
                "message": op.message
            }
            for op in ops
        ]
    finally:
        db.close()
@app.get("/operations", tags=["operations"])
async def list_operations(
    _: str = Depends(verify_api_key),
    limit: int = 20,
):
    db = SessionLocal()
    try:
        ops = db.query(Operation).order_by(Operation.created_at.desc()).limit(limit).all()
        return {
            "operations": [
                {
                    "id": op.id,
                    "type": op.type,
                    "status": op.status,
                    "service": op.service_name,
                    "version": op.version,
                    "target_version": op.target_version,
                    "message": op.message,
                    "error": op.error,
                    "started_at": op.started_at.timestamp() if op.started_at else None,
                    "finished_at": op.finished_at.timestamp() if op.finished_at else None,
                }
                for op in ops
            ],
            "total": db.query(Operation).count(),
        }
    finally:
        db.close()


# ── Endpoints: Operation Status ─────────────────────────────────────────────

@app.get("/operations/{operation_id}", response_model=OperationStatus, tags=["operations"])
async def get_operation(
    operation_id: str,
    _: str = Depends(verify_api_key),
):
    db = SessionLocal()
    try:
        op = db.query(Operation).filter(Operation.id == operation_id).first()
        if not op:
            raise HTTPException(status_code=404, detail="Operation not found.")
        return OperationStatus(
            id=op.id,
            type=op.type,
            status=op.status,
            service=op.service_name or "",
            version=op.version,
            target_version=op.target_version,
            healthcheck_url=op.metadata_json.get("healthcheck_url") if op.metadata_json else None,
            started_at=op.started_at.timestamp() if op.started_at else 0,
            finished_at=op.finished_at.timestamp() if op.finished_at else None,
            message=op.message or "",
            error=op.error,
        )
    finally:
        db.close()


# ── Endpoints: Nodes ─────────────────────────────────────────────────────────

@app.post("/nodes", response_model=RegisterNodeResponse, tags=["nodes"])
async def register_node(
    req: RegisterNodeRequest,
    _: str = Depends(verify_api_key),
):
    db = SessionLocal()
    try:
        node = db.query(Node).filter(
            (Node.name == req.name) | (Node.uuid == req.uuid)
        ).first()

        if not node:
            node = Node(
                name=req.name,
                uuid=req.uuid,
                host=req.host
            )
            db.add(node)
            message = f"Node '{req.name}' registered successfully."
        else:
            node.name = req.name
            node.host = req.host
            message = f"Node '{req.name}' updated successfully."

        db.commit()
        return RegisterNodeResponse(status="success", message=message)
    finally:
        db.close()


@app.get("/nodes", tags=["nodes"])
async def get_nodes(
    _: str = Depends(verify_api_key),
):
    db = SessionLocal()
    try:
        nodes = db.query(Node).all()
        return [
            {
                "name": n.name,
                "uuid": n.uuid,
                "host": n.host,
                "status": n.status,
                "fail_count": n.fail_count,
                "last_seen": n.last_seen,
            }
            for n in nodes
        ]
    finally:
        db.close()
