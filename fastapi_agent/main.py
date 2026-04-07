from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import uvicorn
import asyncio
import json
import os
import sys
import httpx
import time
import hmac
import hashlib
from dotenv import load_dotenv

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from app.logging_utils import setup_logging, bind_request_id

# Configuration
load_dotenv()
logger = setup_logging("heimdall.agent")
STATE_FILE = os.getenv("HEIMDALL_STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json"))
_infra_api_url = os.getenv("INFRA_API_URL")
if not _infra_api_url:
    raise RuntimeError("INFRA_API_URL environment variable must be set for webhook delivery.")
CONTROL_PLANE_WEBHOOK_URL = f"{_infra_api_url.rstrip('/')}/webhook"
_secret_value = os.getenv("INFRA_API_KEY")
if not _secret_value:
    raise RuntimeError("INFRA_API_KEY environment variable must be set for webhook signing.")
SECRET_KEY = _secret_value.encode()
ALLOW_INSECURE_SIGNATURES = os.getenv("HEIMDALL_ALLOW_INSECURE_SIGNATURES", "").strip().lower() in {"1", "true", "yes"}
DISABLE_BG_TASKS = os.getenv("HEIMDALL_AGENT_DISABLE_BG", "").strip().lower() in {"1", "true", "yes"}

# State dictionaries
service_state = {}
service_locks = {}

webhook_client: httpx.AsyncClient = None

def generate_signature(body_str: str, timestamp: str) -> str:
    message = body_str + timestamp
    return hmac.new(SECRET_KEY, message.encode(), hashlib.sha256).hexdigest()

def verify_signature(body_str: str, timestamp: str, signature: str) -> bool:
    expected = generate_signature(body_str, timestamp)
    return hmac.compare_digest(expected, signature)

async def send_webhook(payload: dict):
    if not webhook_client:
        return
    body_str = json.dumps(payload)
    timestamp = str(int(time.time()))
    signature = generate_signature(body_str, timestamp)
    
    headers = {
        "X-Timestamp": timestamp,
        "X-Signature": signature,
        "Content-Type": "application/json"
    }
    try:
        await webhook_client.post(CONTROL_PLANE_WEBHOOK_URL, content=body_str.encode(), headers=headers)
    except Exception as e:
        print(f"Failed to send webhook: {e}")

async def send_heartbeat():
    while True:
        try:
            await asyncio.sleep(10)
            payload = {
                "type": "node_status",
                "services": service_state
            }
            await send_webhook(payload)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Heartbeat error: {e}")

async def health_check_loop():
    while True:
        try:
            await asyncio.sleep(5)
            for svc, data in service_state.items():
                pid = data.get("pid")
                health_url = data.get("health_url")
                status = None

                if pid:
                    try:
                        os.kill(pid, 0)
                        status = "healthy"
                    except OSError:
                        status = "dead"

                if health_url:
                    try:
                        async with httpx.AsyncClient(timeout=2.0) as client:
                            res = await client.get(health_url)
                            if res.status_code == 200:
                                status = "healthy"
                            else:
                                status = "unhealthy"
                    except Exception:
                        status = "unhealthy"

                if status and data.get("status") != status:
                    service_state[svc]["status"] = status
                    save_state()
                    await send_webhook({"type": "status", "service": svc, "status": status})
                    if status == "dead" and svc in service_locks and service_locks[svc].locked():
                        service_locks[svc].release()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Health check error: {e}")

log_buffer = []
LOG_DIR = os.getenv(
    "HEIMDALL_LOG_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
)

async def flush_logs_loop():
    while True:
        try:
            await asyncio.sleep(5)
            if log_buffer:
                to_send = list(log_buffer)
                log_buffer.clear()
                await send_webhook({
                    "type": "logs_batch",
                    "logs": to_send
                })
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Log flush error: {e}")

async def read_stream(stream, service: str, stream_name: str):
    """Read logs from process stdout/stderr, print locally, write to file, and buffer for webhook."""
    if stream is None:
        return
        
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file_path = os.path.join(LOG_DIR, f"{service}.log")
    while True:
        line = await stream.readline()
        if not line:
            break
        log_line = line.decode(errors='replace').rstrip()
        
        # 1. Print locally (goes to node.log)
        print(f"[{service} | {stream_name}] {log_line}")
        
        # 2. Append to individual service log file
        try:
            with open(log_file_path, "a") as f:
                f.write(f"[{stream_name}] {log_line}\n")
        except Exception as e:
            print(f"Failed to write to {log_file_path}: {e}")
            
        # 3. Buffer for Control Plane webhook
        log_buffer.append({
            "service": service,
            "stream": stream_name,
            "log": log_line
        })

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(service_state, f, indent=2)
    except Exception as e:
        print(f"Failed to save state: {e}")

class CommandRequest(BaseModel):
    operation_id: str
    service: str
    flake: str | None = None
    healthcheck_url: str | None = None

class InspectRequest(BaseModel):
    flake: str

def _resolve_flake_cwd(flake_ref: str) -> str | None:
    flake_cwd = None
    if flake_ref.startswith("path:"):
        flake_cwd = flake_ref[len("path:"):]
    elif flake_ref.startswith(("/", "./", "../", "~")):
        flake_cwd = flake_ref
    if flake_cwd:
        flake_cwd = os.path.abspath(os.path.expanduser(flake_cwd))
    return flake_cwd

async def _current_system() -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nix", "eval", "--raw", "--impure", "--expr", "builtins.currentSystem",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, _err = await proc.communicate()
        if proc.returncode == 0:
            return out.decode(errors="replace").strip() or None
    except Exception:
        return None
    return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting up FastAPI node agent...")
    global webhook_client
    if not DISABLE_BG_TASKS:
        webhook_client = httpx.AsyncClient(timeout=5.0)
        heartbeat_task = asyncio.create_task(send_heartbeat())
        health_task = asyncio.create_task(health_check_loop())
        log_flush_task = asyncio.create_task(flush_logs_loop())
    else:
        heartbeat_task = None
        health_task = None
        log_flush_task = None
    
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                
            for svc, data in state.items():
                pid = data.get("pid")
                health_url = data.get("health_url")
                if pid:
                    try:
                        os.kill(pid, 0)
                        service_state[svc] = {"pid": pid, "status": "healthy", "health_url": health_url}
                        lock = asyncio.Lock()
                        await lock.acquire() 
                        service_locks[svc] = lock
                    except OSError:
                        service_state[svc] = {"pid": pid, "status": "dead", "health_url": health_url}
            
            save_state()
        except Exception as e:
            print(f"Error loading state: {e}")

    yield
    print("Shutting down FastAPI node agent...")
    if heartbeat_task:
        heartbeat_task.cancel()
    if health_task:
        health_task.cancel()
    if log_flush_task:
        log_flush_task.cancel()
    if webhook_client:
        await webhook_client.aclose()

app = FastAPI(lifespan=lifespan)

from fastapi import Depends

async def verify_hmac(request: Request):
    x_timestamp = request.headers.get("x-timestamp")
    x_signature = request.headers.get("x-signature")
    
    if not x_timestamp or not x_signature:
        raise HTTPException(status_code=401, detail="Missing HMAC headers")
        
    try:
        ts = int(x_timestamp)
        now = time.time()
        if abs(now - ts) > 30:
            raise HTTPException(status_code=400, detail="Request too old")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid timestamp")
        
    body_bytes = await request.body()
    body_str = body_bytes.decode('utf-8')
    timestamp = x_timestamp
    message = body_str + timestamp
    
    expected_signature = hmac.new(
        SECRET_KEY,
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    
    # Allow a bypass 'default-signature' for easier manual testing
    if ALLOW_INSECURE_SIGNATURES and x_signature == "default-signature":
        return
        
    if not hmac.compare_digest(expected_signature, x_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID")
    if not request_id:
        request_id = str(int(time.time() * 1000))
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

@app.get("/heartbeat")
async def heartbeat():
    return {"status": "ok"}

@app.post("/command", dependencies=[Depends(verify_hmac)])
async def receive_command(command: CommandRequest):
    
    print(f"Received request: {command.model_dump_json(indent=2)}")
    
    svc = command.service
    if svc not in service_locks:
        service_locks[svc] = asyncio.Lock()
        
    lock = service_locks[svc]
    
    if lock.locked():
        old_pid = service_state.get(svc, {}).get("pid")
        if old_pid:
            try:
                os.kill(old_pid, 15)  # SIGTERM
                print(f"Sent SIGTERM to old {svc} process PID {old_pid} for redeploy")
            except OSError:
                pass
        
        try:
            await asyncio.wait_for(lock.acquire(), timeout=10.0)
        except asyncio.TimeoutError:
            return {"status": "failed", "error": "Old process resisted termination, lock timeout"}
    else:
        await lock.acquire()
    
    try:
        if not command.flake:
            lock.release()
            return {"status": "failed", "error": "missing flake"}

        # Validate flake
        flake_ref = command.flake.split('#')[0]
        flake_cwd = _resolve_flake_cwd(flake_ref)
        print(f"Deploy start: service={svc} flake={command.flake} flake_ref={flake_ref} cwd={flake_cwd or os.getcwd()}")
        show_proc = await asyncio.create_subprocess_exec(
            "nix", "flake", "show", flake_ref,
            cwd=flake_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        show_out, show_err = await show_proc.communicate()
        if show_proc.returncode != 0:
            print("FLAKE SHOW ERROR:", show_err.decode(errors='replace'))
            lock.release()
            return {"status": "failed", "error": "invalid flake"}

        # Execute service
        process = await asyncio.create_subprocess_exec(
            "nix", "run", command.flake,
            cwd=flake_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        print(f"Started nix run pid={process.pid} service={svc} flake={command.flake} cwd={flake_cwd or os.getcwd()}")
        
        service_state[svc] = {
            "pid": process.pid, 
            "status": "booting",
            "health_url": command.healthcheck_url
        }
        save_state()
        
        await send_webhook({"type": "status", "service": svc, "status": "booting"})
        
        if process.stdout:
            asyncio.create_task(read_stream(process.stdout, svc, "stdout"))
            
        async def wait_and_release():
            returncode = await process.wait()
            if svc in service_state:
                if returncode == 0:
                    # Process exited cleanly; health checks (if any) should determine status.
                    service_state[svc]["pid"] = None
                    service_state[svc]["status"] = "stopped"
                else:
                    service_state[svc]["status"] = "dead"
                save_state()
            if lock.locked():
                lock.release()
            await send_webhook({
                "type": "status",
                "service": svc,
                "status": "failed" if returncode != 0 else "stopped",
                "error": None if returncode == 0 else f"exit code {returncode}",
            })
            
        asyncio.create_task(wait_and_release())
        
        return {"status": "accepted"}
        
    except Exception as e:
        print(f"Error starting process for service {svc}: {e}")
        lock.release()
        await send_webhook({"type": "status", "service": svc, "status": "failed", "error": str(e)})
        return {"status": "failed"}

@app.post("/inspect", dependencies=[Depends(verify_hmac)])
async def inspect_flake(req: InspectRequest):
    flake_ref = req.flake.split('#')[0]
    flake_cwd = _resolve_flake_cwd(flake_ref)
    manifest_ref = f"{flake_ref}#heimdall-manifest"
    try:
        eval_proc = await asyncio.create_subprocess_exec(
            "nix", "eval", manifest_ref, "--json",
            cwd=flake_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        eval_out, eval_err = await eval_proc.communicate()
        if eval_proc.returncode != 0:
            return {"status": "failed", "error": eval_err.decode(errors="replace")}
        try:
            manifest = json.loads(eval_out.decode(errors="replace"))
        except json.JSONDecodeError as e:
            return {"status": "failed", "error": f"Invalid JSON in manifest: {e}"}
        # If manifest is per-system, resolve to current system
        if isinstance(manifest, dict) and "commands" not in manifest:
            system = await _current_system()
            if system and system in manifest and isinstance(manifest[system], dict):
                manifest = manifest[system]
            elif len(manifest) == 1:
                only_val = next(iter(manifest.values()))
                if isinstance(only_val, dict):
                    manifest = only_val
        return {"status": "success", "manifest": manifest}
    except Exception as e:
        return {"status": "failed", "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    agent_port = int(os.getenv("HEIMDALL_AGENT_PORT", 8001))
    print(f"Heimdall Node Agent starting up on port {agent_port}...")
    uvicorn.run(app, host="0.0.0.0", port=agent_port)
