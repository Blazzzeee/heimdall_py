import os
import asyncio
import pytest

os.environ.setdefault("INFRA_API_KEY", "test-secret")
os.environ.setdefault("HEIMDALL_AGENT_DISABLE_BG", "1")

from fastapi_agent.main import CommandRequest, receive_command, service_locks, service_state


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

@pytest.fixture
def setup_agent(monkeypatch):
    # Clean up state before each test
    service_locks.clear()
    service_state.clear()

    async def fake_subprocess_exec(*args, **kwargs):
        # Simulate nix flake show / eval / run
        cmd = " ".join(args)
        if "flake show" in cmd:
            return DummyProc(returncode=0, stdout_data=b"{}")
        if "nix eval" in cmd:
            return DummyProc(returncode=0, stdout_data=b"{}")
        return DummyProc(returncode=0, stdout_data=b"{}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)
    yield

@pytest.mark.anyio
async def test_command_success(setup_agent):
    """Test normal successful subprocess spawn."""
    payload = CommandRequest(
        operation_id="op-1",
        service="success-svc",
        flake="nixpkgs#hello"
    )
    res = await receive_command(payload)
    
    assert res == {"status": "accepted"}
    assert "success-svc" in service_state

@pytest.mark.anyio
async def test_command_already_running(setup_agent, monkeypatch):
    """Test lock timeout rejection."""
    lock = asyncio.Lock()
    await lock.acquire()
    service_locks["locked-svc"] = lock

    async def fake_wait_for(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    payload = CommandRequest(
        operation_id="op-3",
        service="locked-svc",
        flake="nixpkgs#long-running"
    )
    res = await receive_command(payload)
    
    assert res["status"] == "failed"
