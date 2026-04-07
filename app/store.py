"""
Simple in-memory store for operation state.
In production, replace with Redis or a DB-backed store.
"""

from typing import Dict, Any

operation_store: Dict[str, Any] = {}