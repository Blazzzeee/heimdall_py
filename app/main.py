"""
Infra Control API — Deploy / Teardown / Rollback
Authentication: Bearer API Key via X-API-Key header
"""

from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uuid
import time

from app.models import (
    DeployRequest, DeployResponse,
    TeardownRequest, TeardownResponse,
    RollbackRequest, RollbackResponse,
    OperationStatus,
)
from app.store import operation_store
from app.auth import verify_api_key
from app.ops import run_deploy, run_teardown, run_rollback


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Infra Control API starting up...")
    yield
    print("Infra Control API shutting down...")


app = FastAPI(
    title="Infra Control API",
    description="Conversational infrastructure control — deploy, teardown, rollback.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "timestamp": time.time()}


# ── Deploy ────────────────────────────────────────────────────────────────────

@app.post("/deploy", response_model=DeployResponse, tags=["operations"])
async def deploy(
    req: DeployRequest,
    background_tasks: BackgroundTasks,
    _: str = Depends(verify_api_key),
):
    op_id = str(uuid.uuid4())
    operation_store[op_id] = {
        "id": op_id,
        "type": "deploy",
        "status": "pending",
        "service": req.service,
        "version": req.version,
        "environment": req.environment,
        "started_at": time.time(),
        "finished_at": None,
        "message": "Queued",
        "error": None,
    }
    background_tasks.add_task(run_deploy, op_id, req)
    return DeployResponse(
        operation_id=op_id,
        status="pending",
        message=f"Deploy of {req.service}@{req.version} to {req.environment} queued.",
    )


# ── Teardown ──────────────────────────────────────────────────────────────────

@app.post("/teardown", response_model=TeardownResponse, tags=["operations"])
async def teardown(
    req: TeardownRequest,
    background_tasks: BackgroundTasks,
    _: str = Depends(verify_api_key),
):
    op_id = str(uuid.uuid4())
    operation_store[op_id] = {
        "id": op_id,
        "type": "teardown",
        "status": "pending",
        "service": req.service,
        "environment": req.environment,
        "started_at": time.time(),
        "finished_at": None,
        "message": "Queued",
        "error": None,
    }
    background_tasks.add_task(run_teardown, op_id, req)
    return TeardownResponse(
        operation_id=op_id,
        status="pending",
        message=f"Teardown of {req.service} in {req.environment} queued.",
    )


# ── Rollback ──────────────────────────────────────────────────────────────────

@app.post("/rollback", response_model=RollbackResponse, tags=["operations"])
async def rollback(
    req: RollbackRequest,
    background_tasks: BackgroundTasks,
    _: str = Depends(verify_api_key),
):
    op_id = str(uuid.uuid4())
    operation_store[op_id] = {
        "id": op_id,
        "type": "rollback",
        "status": "pending",
        "service": req.service,
        "target_version": req.target_version,
        "environment": req.environment,
        "started_at": time.time(),
        "finished_at": None,
        "message": "Queued",
        "error": None,
    }
    background_tasks.add_task(run_rollback, op_id, req)
    return RollbackResponse(
        operation_id=op_id,
        status="pending",
        message=f"Rollback of {req.service} to {req.target_version} in {req.environment} queued.",
    )


# ── Operation Status ──────────────────────────────────────────────────────────

@app.get("/operations/{operation_id}", response_model=OperationStatus, tags=["operations"])
async def get_operation(
    operation_id: str,
    _: str = Depends(verify_api_key),
):
    op = operation_store.get(operation_id)
    if not op:
        raise HTTPException(status_code=404, detail="Operation not found.")
    return OperationStatus(**op)


@app.get("/operations", tags=["operations"])
async def list_operations(
    _: str = Depends(verify_api_key),
    limit: int = 20,
):
    ops = sorted(operation_store.values(), key=lambda x: x["started_at"], reverse=True)
    return {"operations": ops[:limit], "total": len(ops)}