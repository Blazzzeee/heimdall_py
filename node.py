from fastapi import FastAPI
from datetime import datetime, UTC

app = FastAPI()

@app.get("/heartbeat")
async def heartbeat():
    # Node reports it is alive
    return {
        "status": "alive",
        "time": datetime.now(UTC).isoformat() + "Z"
    }

