# -*- coding: utf-8 -*-
"""Capability registry — the controlled whitelist of business actions.

PRD FR-014/FR-015: every executable business action must be declared here
with its target model, parameter schema, risk level, confirmation policy
and an executor that ultimately calls an *existing* Odoo business method.
The LLM never sees raw ORM; it can only emit a capability_id + params that
must pass schema validation and the execution guard.
"""
from typing import Any, Callable, Dict, List, Optional

from .schema import ParamSchema


class Capability:
    __slots__ = (
        "capability_id", "name", "description", "target_model", "action",
        "schema", "groups", "risk", "require_confirmation", "enabled",
        "precondition", "executor", "result_check", "scenario", "company_dependent",
    )

    def __init__(self, capability_id, name, description, target_model, action,
                 schema: Optional[ParamSchema] = None, groups=(),
                 risk="L0", require_confirmation=False, enabled=True,
                 precondition: Optional[Callable] = None,
                 executor: Optional[Callable] = None,
                 result_check: Optional[Callable] = None,
                 scenario="", company_dependent=True):
        if risk not in ("L0", "L1", "L2", "L3"):
            raise ValueError(f"invalid risk level {risk!r}")
        self.capability_id = capability_id
        self.name = name
        self.description = description
        self.target_model = target_model
        self.action = action
        self.schema = schema or ParamSchema([])
        self.groups = tuple(groups)
        self.risk = risk
        self.require_confirmation = require_confirmation
        # L3 capabilities ship disabled: an admin must explicitly enable them.
        self.enabled = enabled and risk != "L3"
        self.precondition = precondition
        self.executor = executor
        self.result_check = result_check
        self.scenario = scenario
        self.company_dependent = company_dependent

    def to_public_dict(self) -> Dict[str, Any]:
        """What we expose to the frontend and to the LLM prompt."""
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "description": self.description,
            "target_model": self.target_model,
            "action": self.action,
            "risk": self.risk,
            "require_confirmation": self.require_confirmation,
            "params": self.schema.to_dict(),
            "scenario": self.scenario,
        }


class CapabilityRegistry:
    def __init__(self):
        self._caps: Dict[str, Capability] = {}

    def register(self, cap: Capability) -> Capability:
        if cap.capability_id in self._caps:
            raise ValueError(f"duplicate capability id {cap.capability_id}")
        self._caps[cap.capability_id] = cap
        return cap

    def get(self, capability_id: str) -> Optional[Capability]:
        return self._caps.get(capability_id)

    def require(self, capability_id: str) -> Capability:
        cap = self._caps.get(capability_id)
        if cap is None:
            raise KeyError(f"capability {capability_id!r} is not registered")
        return cap

    def all(self) -> List[Capability]:
        return list(self._caps.values())

    def for_scenario(self, scenario: str) -> List[Capability]:
        return [c for c in self._caps.values() if not scenario or not c.scenario or c.scenario == scenario]

    def ids(self) -> List[str]:
        return sorted(self._caps)


# Module-level singleton; scenario addons import and register into it.
REGISTRY = CapabilityRegistry()


def capability(**kwargs) -> Callable:
    """Class/function decorator sugar: @capability(capability_id=..., ...)"""
    def wrapper(obj):
        cap = Capability(**kwargs)
        REGISTRY.register(cap)
        return obj
    return wrapper
