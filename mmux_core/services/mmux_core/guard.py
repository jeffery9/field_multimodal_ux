# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Execution guard: the server-side funnel every business action must pass.

Implements PRD FR-016's ordered pipeline and FR-017's result state machine.
The guard itself is pure logic; the Odoo glue (env, models, transactions)
is supplied by the caller via `BackendOps`, so the ordering and decision
logic stays unit-testable outside Odoo.
"""
import enum
import logging
from typing import Any, Dict, Optional

from .capability import Capability, CapabilityRegistry
from .schema import ValidationError

_logger = logging.getLogger(__name__)


class Risk(str, enum.Enum):
    L0 = "L0"  # query
    L1 = "L1"  # low-risk record
    L2 = "L2"  # business state change
    L3 = "L3"  # high risk


class ExecStatus(str, enum.Enum):
    RECOGNIZED = "recognized"            # input parsed, nothing validated
    VALIDATED = "validated"              # schema + whitelist + ACL + precondition OK
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SUBMITTED = "submitted"              # business call started
    SUCCESS = "success"                  # business call returned and result verified
    FAILED = "failed"                    # validation or execution failed (safe)
    UNKNOWN = "unknown"                  # result uncertain: verify, do NOT blind-retry


RISK_CONFIRMATION = {
    Risk.L0: False,
    Risk.L1: False,
    Risk.L2: True,
    Risk.L3: True,  # plus requires admin enablement
}


class GuardError(Exception):
    def __init__(self, status: ExecStatus, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


class ExecutionGuard:
    """Orchestrates the FR-016 pipeline against an injected backend.

    BackendOps contract (implemented by mmux_core/models for Odoo):
      - has_user_group(group_xmlid) -> bool
      - check_access(cap, record_id) -> record   (ACL + record rules + company isolation)
      - precondition(cap, record, params) -> None  (raises GuardError on state violation)
      - execute(cap, record, params) -> Any      (calls the existing business method)
      - verify_result(cap, record, result) -> dict
      - is_duplicate(idempotency_key) -> bool / mark_executed(key, status)
    """

    def __init__(self, registry: CapabilityRegistry, backend):
        self.registry = registry
        self.backend = backend

    # ------------------------------------------------------------------ #
    def validate(self, capability_id: str, params: Dict[str, Any],
                 record_id: Optional[int] = None,
                 idempotency_key: Optional[str] = None,
                 confirmed: bool = False) -> Dict[str, Any]:
        """Steps 3-7 of FR-016: returns a validated plan or raises GuardError.

        Returns dict with keys: capability, record, params, status
        (VALIDATED or AWAITING_CONFIRMATION), confirmation payload.
        """
        # 4. capability whitelist
        try:
            cap = self.registry.require(capability_id)
        except KeyError as exc:
            raise GuardError(ExecStatus.FAILED, str(exc), {"capability_id": capability_id})
        if not cap.enabled:
            raise GuardError(ExecStatus.FAILED,
                             f"capability {capability_id} is disabled "
                             f"(L3 capabilities must be explicitly enabled by an administrator)")

        # 3. parameter schema validation (strict: unknown keys rejected)
        try:
            clean_params = cap.schema.validate(params or {})
        except ValidationError as exc:
            raise GuardError(ExecStatus.FAILED, f"invalid parameters: {exc}",
                             {"field": exc.field})

        # idempotency pre-check (6.4): a completed key is never re-executed
        if idempotency_key and self.backend.is_duplicate(idempotency_key):
            raise GuardError(ExecStatus.FAILED,
                             "duplicate request: this idempotency key was already processed",
                             {"idempotency_key": idempotency_key})

        # 5. ACL / record rules / company isolation — delegated to Odoo
        record = None
        if record_id is not None:
            record = self.backend.check_access(cap, record_id)

        # groups
        for group in cap.groups:
            if not self.backend.has_user_group(group):
                raise GuardError(ExecStatus.FAILED,
                                 f"missing required group {group} for {capability_id}")

        # 6. native business precondition / state check
        if record is not None:
            self.backend.precondition(cap, record, clean_params)

        # 7. risk policy -> confirmation gate
        needs_confirmation = cap.require_confirmation or RISK_CONFIRMATION[Risk(cap.risk)]
        if needs_confirmation and not confirmed:
            return {
                "capability": cap,
                "record": record,
                "params": clean_params,
                "status": ExecStatus.AWAITING_CONFIRMATION,
                "confirmation": self._confirmation_payload(cap, record, clean_params),
            }
        return {"capability": cap, "record": record, "params": clean_params,
                "status": ExecStatus.VALIDATED, "confirmation": None}

    # ------------------------------------------------------------------ #
    def execute(self, plan: Dict[str, Any], idempotency_key: Optional[str] = None,
                confirmed: bool = False) -> Dict[str, Any]:
        """Steps 7-10. The plan must come from validate() *in the same
        request* — a confirmed payload whose params/target changed must be
        re-validated (FR §6.3)."""
        cap, record, params = plan["capability"], plan["record"], plan["params"]
        if plan["status"] == ExecStatus.AWAITING_CONFIRMATION and not confirmed:
            return {"status": ExecStatus.AWAITING_CONFIRMATION,
                    "confirmation": plan["confirmation"]}

        if idempotency_key and self.backend.is_duplicate(idempotency_key):
            raise GuardError(ExecStatus.FAILED, "duplicate request (idempotency key reused)")

        # 8. call the existing business method
        self.backend.mark_executed(idempotency_key, ExecStatus.SUBMITTED.value
                                   if idempotency_key else None)
        try:
            result = self.backend.execute(cap, record, params)
        except GuardError:
            if idempotency_key:
                self.backend.mark_executed(idempotency_key, ExecStatus.FAILED.value)
            raise
        except Exception as exc:
            # 9. result unknown is NOT success — caller must verify (FR-017)
            if idempotency_key:
                self.backend.mark_executed(idempotency_key, ExecStatus.UNKNOWN.value)
            _logger.exception("capability %s execution error", cap.capability_id)
            raise GuardError(ExecStatus.UNKNOWN,
                             f"execution outcome uncertain: {exc}; verify the record "
                             f"state before retrying — do not blindly resubmit")

        # 9. verify actual result
        payload = self.backend.verify_result(cap, record, result)
        if idempotency_key:
            self.backend.mark_executed(idempotency_key, ExecStatus.SUCCESS.value)
        # 10. audit trail is written by the backend wrapper
        return {"status": ExecStatus.SUCCESS, "result": payload}

    # ------------------------------------------------------------------ #
    @staticmethod
    def _confirmation_payload(cap: Capability, record, params) -> Dict[str, Any]:
        return {
            "capability_id": cap.capability_id,
            "action": cap.action,
            "target_model": cap.target_model,
            "record": getattr(record, "display_name", None) if record is not None else None,
            "params": params,
            "risk": cap.risk,
            "impact": cap.description,
        }
