# -*- coding: utf-8 -*-
"""Idempotency helpers (PRD §6.4)."""
import uuid


def idempotency_key(prefix: str = "mmux") -> str:
    """Fresh client-side idempotency key. One key per user intent, reused
    across retries of the *same* intent so the server can deduplicate."""
    return f"{prefix}:{uuid.uuid4()}"
