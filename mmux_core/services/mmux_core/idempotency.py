# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Idempotency helpers (PRD §6.4)."""
import uuid


def idempotency_key(prefix: str = "mmux") -> str:
    """Fresh client-side idempotency key. One key per user intent, reused
    across retries of the *same* intent so the server can deduplicate."""
    return f"{prefix}:{uuid.uuid4()}"
