from pydantic import BaseModel, Field
from typing import Optional, Literal
import time


# ── Requests ──────────────────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    service: str = Field(..., examples=["api-gateway"])
    node_name: str | None = Field(None, examples=["node-1"])
    repo_url: str | None = Field(None, examples=["github:org/repo"])
    flake: str | None = Field(None, examples=["github:org/repo#api"])
    commands: list[str] | None = Field(None, examples=[["run", "migrate"]])
    healthcheck_url: str | None = Field(None, examples=["http://localhost:8080/"])
    version: str = Field("latest", examples=["v1.4.2"])
    environment: Literal["dev", "staging", "prod"] = "dev"
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
    environment: Literal["dev", "staging", "prod"] = "dev"
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
    environment: str
    started_at: float
    finished_at: Optional[float]
    message: str
    error: Optional[str]
    version: Optional[str] = None
    target_version: Optional[str] = None
    healthcheck_url: str | None = None
