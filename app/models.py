from pydantic import BaseModel, Field
from typing import Optional, Literal


# ── Requests ──────────────────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    service: str = Field(..., examples=["api-gateway"])
    node_name: str | None = Field(None, examples=["node-1"])
    commands: list[str] | None = Field(None, examples=[["run", "migrate"]])
    version: str = Field("latest", examples=["v1.4.2"])
    triggered_by: str | None = None

class DeclareServiceRequest(BaseModel):
    service: str = Field(..., examples=["api-gateway"])
    node_name: str = Field(..., examples=["node-1"])
    repo_url: str | None = None
    flake: str | None = None
    triggered_by: str | None = None

class TeardownRequest(BaseModel):
    service: str = Field(..., examples=["api-gateway"])
    triggered_by: str | None = None

class RollbackRequest(BaseModel):
    service: str = Field(..., examples=["api-gateway"])
    target_version: str = Field(..., examples=["v1.4.1"])
    reason: Optional[str] = None
    triggered_by: str | None = None

class DeployAllResponse(BaseModel):
    status: str
    message: str
    operation_ids: list[str]


class RegisterNodeRequest(BaseModel):
    name: str = Field(..., examples=["node-1"])
    uuid: str = Field(..., examples=["node-1-unique-id"])
    host: str = Field(..., examples=["http://10.0.0.5:8001"])

class RegisterNodeResponse(BaseModel):
    status: str
    message: str


# ── Responses ─────────────────────────────────────────────────────────────────

class DeployResponse(BaseModel):
    operation_id: str
    status: str
    message: str

class CommandRequest(BaseModel):
    service: str = Field(..., examples=["api-gateway"])
    command: str = Field(..., examples=["deploy", "teardown"])
    node_name: str | None = Field(None, examples=["node-1"])
    triggered_by: str | None = None

class CommandResponse(BaseModel):
    operation_id: str
    status: str
    message: str

class TeardownResponse(BaseModel):
    operation_id: str
    status: str
    message: str

class RollbackResponse(BaseModel):
    operation_id: str
    status: str
    message: str


# ── Operation Status ──────────────────────────────────────────────────────────

class OperationStatus(BaseModel):
    id: str
    type: str
    status: Literal["pending", "running", "success", "failed"]
    service: str
    started_at: float
    finished_at: Optional[float]
    message: str
    error: Optional[str]
    version: Optional[str] = None
    target_version: Optional[str] = None
    healthcheck_url: str | None = None
