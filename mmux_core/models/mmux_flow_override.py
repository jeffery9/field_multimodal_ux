# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Data-driven flow overrides (v19.0.6).

What this model is for
----------------------
A guided flow (`TaskFlow`) is code-defined: its builders query live Odoo
records, which is exactly why the four shipped scenario flows live in
Python. But "the picking flow at THIS site has an extra scan step" should
not be a code deployment.

A row in this table with `flow_id = "wh_guided_picking"` REPLACES the
code-registered flow of the same id wherever flows are resolved — the
service layer funnels every lookup through `mmux.service._flow_for`,
which prefers a DB row and falls back to `FLOW_ENGINE.get`. Code flows
are untouched and remain the fallback: delete the row and the code flow
is back, byte for byte.

What an override CANNOT do
--------------------------
* It cannot widen authority. A step may name a `capability_id`, but that
  capability must exist in the code-defined registry (validated here at
  save time) and it still goes through the same `ExecutionGuard` — the
  same whitelist, the same L2 confirmation. An override changes what the
  operator is ASKED to do, never what the system is allowed to do.
* It cannot invent step types: `input_type` is validated against the
  same closed set the pure core enforces (`GuidedStep.__init__`
  re-validates too — defence in depth).
* Steps are static rows, not builders: an override cannot query records.
  A flow that must follow live `move_line_ids` belongs in code; the DB
  form is for reordering, rewording, adding confirmation/scan steps.

Mid-session stability (deliberate): the agent snapshots a flow's recipe
into the loop blob once per loop, "so the script the operator hears
cannot shift mid-task" (see `_agent_observe`). Editing an override
therefore affects NEW sessions only; in-flight sessions run the steps
they started with. That is the intended behaviour, not a stale-cache
bug.
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..services.mmux_core import (
    CAPABILITY_REGISTRY, GuidedStep, TaskFlow,
)

#: `GuidedStep` constructor kwargs an override step may carry. Exactly
#: the fields of `GuidedStep.to_dict()` minus `done` (cursor state, never
#: authored) — the override stores steps in the same shape the recipe
#: snapshot uses, so round-tripping is lossless.
_STEP_KEYS = ("step_id", "title", "voice_prompt", "input_type",
              "capability_id", "params", "record_ref", "skippable",
              "hint", "expected", "skill")
_INPUT_TYPES = ("scan", "voice_qty", "confirm", "capability")


def _validate_steps(steps):
    """Shared validator for the constraint and for `to_taskflow`'s
    defensive re-check. Returns the normalised list of step dicts."""
    if not isinstance(steps, list) or not steps:
        raise ValidationError(
            "steps must be a non-empty JSON list of step objects.")
    seen = set()
    for raw in steps:
        if not isinstance(raw, dict):
            raise ValidationError("Each step must be a JSON object.")
        for key in ("step_id", "title", "voice_prompt"):
            if not raw.get(key):
                raise ValidationError(
                    "Every step needs %s." % key)
        step_id = raw["step_id"]
        if step_id in seen:
            raise ValidationError(
                "Duplicate step_id %r — step ids must be unique."
                % step_id)
        seen.add(step_id)
        input_type = raw.get("input_type") or "confirm"
        if input_type not in _INPUT_TYPES:
            raise ValidationError(
                "step %r: input_type must be one of %s (got %r)."
                % (step_id, ", ".join(_INPUT_TYPES), input_type))
        capability_id = raw.get("capability_id")
        if capability_id:
            cap = CAPABILITY_REGISTRY.get(capability_id)
            if cap is None:
                raise ValidationError(
                    "step %r names capability %r, which is not in the "
                    "code-defined whitelist." % (step_id, capability_id))
            if not cap.enabled:
                raise ValidationError(
                    "step %r names capability %r, which is currently "
                    "disabled (see Capability Switches)."
                    % (step_id, capability_id))
    return steps


class MmuxFlowOverride(models.Model):
    _name = "mmux.flow.override"
    _description = "Guided flow override (same flow_id wins over code)"
    _order = "flow_id"

    flow_id = fields.Char(
        "Flow id", required=True, index=True,
        help="The code-registered flow this row replaces. Same id = the "
             "row wins; delete the row and the code flow is back.")
    # name/description are Json, same as steps: each is either a plain
    # string or a {lang: text} mapping, resolved per operator language by
    # the same resolve_text chain used everywhere else.
    name = fields.Json(
        "Name", required=True,
        help="Plain text or {lang: text} mapping, e.g. "
             '{"zh_CN": "…", "en_US": "…"}.')
    description = fields.Json(
        "Description",
        help="Plain text or {lang: text} mapping (optional).")
    voice_on = fields.Boolean(
        "Voice on", default=True,
        help="Whether step prompts are spoken aloud.")
    steps = fields.Json(
        "Steps", required=True,
        help="JSON list of step objects: step_id, title, voice_prompt, "
             "input_type (scan/voice_qty/confirm/capability), and "
             "optionally capability_id, params, record_ref, skippable, "
             "hint, expected, skill. Text fields accept {lang: text}.")

    _sql_constraints = [
        ("flow_id_uniq", "unique (flow_id)",
         "One override row per flow."),
    ]

    # ------------------------------------------------------------------ #
    # constraints
    # ------------------------------------------------------------------ #
    @api.constrains("steps")
    def _check_steps(self):
        for rec in self:
            _validate_steps(rec.steps)

    # ------------------------------------------------------------------ #
    # bridge to the pure core
    # ------------------------------------------------------------------ #
    def to_taskflow(self):
        """Build the `TaskFlow` this row stands for.

        One builder from static rows — a deliberately simple closure over
        the validated step dicts. `GuidedStep.__init__` re-validates
        `input_type`, so a row that somehow bypassed the constraint (e.g.
        written by an import) still cannot smuggle an unknown step type
        into a session.
        """
        self.ensure_one()
        steps = _validate_steps(self.steps)

        def builder(env, params):
            built = []
            for raw in steps:
                kwargs = {k: raw[k] for k in _STEP_KEYS
                          if k in raw and raw[k] is not None}
                if "input_type" not in kwargs:
                    kwargs["input_type"] = "confirm"
                built.append(GuidedStep(**kwargs))
            return built

        return TaskFlow(
            flow_id=self.flow_id,
            name=self.name,
            steps_builder=builder,
            description=self.description or "",
            voice_on=self.voice_on,
        )
