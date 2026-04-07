#!/usr/bin/env python3
import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from db import SessionLocal, ServiceInstance

def main() -> int:
    db = SessionLocal()
    try:
        svc = db.query(ServiceInstance).filter(ServiceInstance.name == "service-1").first()
        print("flake:", svc.flake if svc else None)
        print("commands:", svc.commands if svc else None)
        return 0
    finally:
        db.close()

if __name__ == "__main__":
    raise SystemExit(main())
