# heimdall

**Distributed Node Orchestration & Health Monitoring Platform**
Instead of managing runtime state, Heimdall derives it: infra becomes a reproducible system graph evaluated across nodes. Operations become messaging (ChatOps as the control plane), failures are modeled and handled automatically, and recovery is built-in. It unifies provisioning, deployment, monitoring, and rollback with health-aware execution, structured debugging, and a REST API for live node state.

---

## Features

- **Agent + control plane** — agents run workloads and stream state to the control plane
- **Signed webhooks** — agent → control plane updates are HMAC‑signed
- **Health + status tracking** — services are marked healthy/dead/failed based on agent signals
- **Log streaming** — agent batches logs to the control plane
- **Reproducible dev environment** — Nix flake for consistent local setup

---

## Architecture

```
┌──────────────────────┐        signed webhooks        ┌──────────────────────────┐
│   Agent (node host)  │ ────────────────────────────▶ │     Control Plane        │
│  - runs services     │                                │   FastAPI + DB           │
│  - health checks     │                                │   /webhook + /services   │
│  - logs batching     │ ◀────────────────────────────  │   API + status tracking  │
└──────────────────────┘        instructions/API        └──────────────────────────┘
```

Agents send `node_status`, `status`, and `logs_batch` events to the control plane’s `/webhook` endpoint. All webhook payloads are HMAC‑signed with `INFRA_API_KEY`.

## Code Map

- `fastapi_agent/main.py` — node agent runtime: health checks, log batching, signed webhook delivery
- `api.py` — control plane API + webhook handler + DB writes
- `app/main.py` — legacy/aux deploy, teardown, rollback endpoints
- `discord_bot/bot.py` — Discord command bot that calls the control plane API
- `start.sh` — control plane launcher (tmux)
- `start-agent.sh` — agent launcher (tmux)

---

## Getting Started

### Prerequisites
- python virtual env
- [Nix](https://nixos.org/) with flakes enabled

```bash
# Enter the dev shell — all dependencies are loaded automatically
direnv allow    # if you use direnv
# or
nix develop
```

```bash
pip install -r requirements.txt
```
---

### Starting project locally
```bash
chmod +x ./start.sh
./start.sh && tmux attach -t heimdall
```

#### While dealing with multiple machines , agent (slave nodes)
```bash
chmod +x ./start-agent.sh
./start-agent.sh && tmux attach -t heimdall-agent
```

## Running Tests

```bash
pytest -q
```
---


## Tech Stack

| Layer         | Technology          |
|---------------|---------------------|
| Web framework | FastAPI             |
| Async HTTP    | httpx               |
| Testing       | pytest              |
| Runtime       | Python asyncio      |
| Dev env       | Nix flakes + direnv |

---

## Contributing

1. Fork the repo and create a branch from `master`
2. Follow the `ft.<feature>` naming convention for feature branches
3. Add or update tests for any changed behaviour
4. Open a pull request with a clear description

---
