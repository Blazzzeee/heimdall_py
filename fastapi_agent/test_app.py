import os
import asyncio
import pytest

os.environ.setdefault("INFRA_API_KEY", "test-secret")
os.environ.setdefault("HEIMDALL_AGENT_DISABLE_BG", "1")

from fastapi_agent.main import CommandRequest, receive_command, service_state


class DummyStream:
    async def readline(self):
        return b""


class DummyProc:
    def __init__(self, returncode: int = 0, stdout_data: bytes = b"{}", stderr_data: bytes = b""):
        self.returncode = returncode
        self._stdout_data = stdout_data
        self._stderr_data = stderr_data
        self.pid = 1234
        self.stdout = DummyStream()

    async def communicate(self):
        return self._stdout_data, self._stderr_data

    async def wait(self):
        return 0


@pytest.mark.anyio
async def test_command_endpoint(monkeypatch):
    service_state.clear()

    async def fake_subprocess_exec(*args, **kwargs):
        return DummyProc(returncode=0, stdout_data=b"{}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)

    payload = CommandRequest(
        operation_id="op-test-123",
        service="test-service-1",
        flake="nixpkgs#hello"
    )
    response = await receive_command(payload)
    assert response == {"status": "accepted"}
    assert "test-service-1" in service_state
