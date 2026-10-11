# -*- coding: utf-8 -*-
"""Guided task flows: voice-driven step-by-step navigation (agent loop
for human tasks).

Concept: a **TaskFlow** is a code-defined recipe that, given the current
Odoo records, yields an ordered list of guided steps. Each step carries:
 - a spoken prompt (TTS text, e.g. "前往货架 A-01，扫描商品 XX")
 - an expected input type (scan / voice_qty / confirm / capability call)
 - an optional capability invocation to advance the step (goes through
   the SAME ExecutionGuard — guided mode never bypasses it).

**Skill level (novice / expert)**: a flow may provide two step builders.
The novice variant is deliberately more granular — it splits composite
actions, adds explanation/"why" steps, adds verification steps and speaks
longer prompts; the expert variant merges actions into fewer steps with
terse prompts. The skill level changes *granularity and verbosity*, never
authority: both variants run the identical capability guard, so L2 steps
still require human confirmation in either mode.
"""
from typing import Any, Callable, Dict, List, Optional

SKILL_LEVELS = ("novice", "expert")


def _text(value):
    """Step text fields may be a plain string (language-neutral) OR a
    {lang: text} mapping for per-language broadcasting. Localization
    happens at the edge (service layer) via resolve_text."""
    return value


class GuidedStep:
    __slots__ = ("step_id", "title", "voice_prompt", "input_type",
                 "capability_id", "params", "record_ref", "skippable", "done",
                 "hint", "expected", "skill")

    def __init__(self, step_id, title, voice_prompt, input_type="confirm",
                 capability_id=None, params=None, record_ref=None,
                 skippable=False, done=False, hint=None, expected=None,
                 skill=None):
        if input_type not in ("scan", "voice_qty", "confirm", "capability"):
            raise ValueError(f"invalid input_type {input_type!r}")
        self.step_id = step_id
        self.title = title
        self.voice_prompt = voice_prompt
        self.input_type = input_type  # what the UI should listen for
        self.capability_id = capability_id
        self.params = params or {}
        self.record_ref = record_ref
        self.skippable = skippable
        self.done = done
        # novice-mode extras: a longer on-screen hint and an expected-value
        # echo so the operator can self-check before submitting.
        self.hint = hint
        self.expected = expected
        self.skill = skill

    def to_dict(self, lang=None):
        """Serialise. When `lang` is given, any {lang: text} fields are
        resolved to that language (fallback chain applied); without it the
        raw (possibly multilingual) values are returned."""
        if lang:
            from .i18n import resolve_text
            return {"step_id": self.step_id,
                    "title": resolve_text(self.title, lang),
                    "voice_prompt": resolve_text(self.voice_prompt, lang),
                    "input_type": self.input_type,
                    "capability_id": self.capability_id,
                    "params": self.params, "record_ref": self.record_ref,
                    "skippable": self.skippable, "done": self.done,
                    "hint": resolve_text(self.hint, lang),
                    "expected": resolve_text(self.expected, lang),
                    "skill": self.skill}
        return {"step_id": self.step_id, "title": self.title,
                "voice_prompt": self.voice_prompt,
                "input_type": self.input_type,
                "capability_id": self.capability_id,
                "params": self.params, "record_ref": self.record_ref,
                "skippable": self.skippable, "done": self.done,
                "hint": self.hint, "expected": self.expected,
                "skill": self.skill}


class TaskFlow:
    """A code-defined guided flow (like Capability/Plan: code first).

    Provide EITHER a single `steps_builder` (same steps for everyone) or
    the pair `steps_builder_novice` / `steps_builder_expert`. The novice
    builder should produce finer-grained, more explanatory steps; the
    expert builder fewer, terse ones. Both must map onto the same
    capabilities so the guard behavior is identical.
    `_keywords` feeds natural-language flow matching."""

    def __init__(self, flow_id, name, steps_builder: Callable = None,
                 description="", voice_on=True,
                 steps_builder_novice: Callable = None,
                 steps_builder_expert: Callable = None):
        if steps_builder is None and not (steps_builder_novice and steps_builder_expert):
            raise ValueError(
                "TaskFlow needs steps_builder or both novice/expert builders")
        self.flow_id = flow_id
        self.name = name
        self.steps_builder = steps_builder
        self.steps_builder_novice = steps_builder_novice
        self.steps_builder_expert = steps_builder_expert
        self.description = description
        self.voice_on = voice_on
        self._keywords: tuple = ()

    @property
    def supports_skill_levels(self):
        return bool(self.steps_builder_novice and self.steps_builder_expert)

    def builder_for(self, skill_level):
        """Pick the builder for a skill level, falling back to the single
        builder when the flow does not differentiate."""
        if skill_level == "novice" and self.steps_builder_novice:
            return self.steps_builder_novice
        if skill_level == "expert" and self.steps_builder_expert:
            return self.steps_builder_expert
        return self.steps_builder or self.steps_builder_novice or self.steps_builder_expert

    def recipe(self, env, params=None, skill_level="novice"):
        """The deterministic reference script, as plain dicts.

        Added in v3.0 for the pure agent loop. In the LLM-first design a
        `TaskFlow` *was* the plan: the server walked these steps and the
        model had no say. With the agent in charge the flow is demoted to
        what it always really was — a **recipe**: the known-good way to do
        this job, authored by the scenario team, containing no model
        output.

        The agent narrates its own way through the work; the recipe is
        what it falls back to when the model is unreachable, invalid or
        spinning. Because the recipe is data (dicts, not `GuidedStep`
        objects) it can be consumed by `agentloop` without that module
        importing this one, keeping the dependency graph one-directional
        and both modules independently testable.

        Read-only by construction: it builds steps and serialises them —
        it never executes, never writes, and never consults the LLM.
        """
        try:
            steps = TaskFlowEngine().build_steps_for(self, env, params,
                                                     skill_level)
        except Exception:
            # a scenario builder that raises on a half-built record (e.g.
            # a picking with no move lines) must not break the fallback:
            # an empty recipe is a valid answer, a crash is not.
            return []
        return [s.to_dict() for s in steps]

    def to_dict(self):
        return {"flow_id": self.flow_id, "name": self.name,
                "description": self.description,
                "supports_skill_levels": self.supports_skill_levels}



class TaskFlowEngine:
    """In-memory session tracking of a guided run. The Odoo layer stores
    the cursor (done step ids) in the interaction log / a dedicated run
    record; the engine here stays pure and stateless per call."""

    def __init__(self):
        self._flows: Dict[str, TaskFlow] = {}

    def register(self, flow: TaskFlow) -> TaskFlow:
        if flow.flow_id in self._flows:
            raise ValueError(f"duplicate flow id {flow.flow_id}")
        self._flows[flow.flow_id] = flow
        return flow

    def get(self, flow_id) -> Optional[TaskFlow]:
        return self._flows.get(flow_id)

    def all(self) -> List[TaskFlow]:
        return list(self._flows.values())

    # -------------------------------------------------------------- #
    def build_steps(self, flow_id, env, params=None,
                    skill_level="novice") -> List[GuidedStep]:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise KeyError(f"flow {flow_id!r} is not registered")
        return self.build_steps_for(flow, env, params, skill_level)

    @staticmethod
    def build_steps_for(flow: "TaskFlow", env, params=None,
                        skill_level="novice") -> List[GuidedStep]:
        """Build the steps of a flow OBJECT (not looked up by id).

        Extracted from `build_steps` in v3.0 so `TaskFlow.recipe()` can
        render its own steps without needing to be registered in the
        engine — the recipe is a property of the flow, not of the
        registry. Behaviour is identical to the id-keyed path (the two
        share this body).
        """
        if skill_level not in SKILL_LEVELS:
            skill_level = "novice"
        builder = flow.builder_for(skill_level)
        steps = builder(env, params or {})
        if not steps:
            return []
        # validate step ids unique and tag the skill level on each step
        ids = set()
        for s in steps:
            if s.step_id in ids:
                raise ValueError(f"duplicate step_id {s.step_id}")
            ids.add(s.step_id)
            if s.skill is None:
                s.skill = skill_level
        return steps

    @staticmethod
    def _current_step(steps: List[GuidedStep], done_ids):
        """The cursor: the first step not in `done_ids`, or None.

        Extracted so `cursor_view` and `advance` share ONE definition of
        "where am I".  They used to disagree in cost: `advance` called
        `cursor_view` just to read `view["current"]["step_id"]`, which
        serialised every step (all of their titles, prompts and hints,
        in the requested language) to answer a question about a single
        one.  Returning the *object* lets callers serialise only what
        they actually need.

        Returns a `GuidedStep`, not a dict, and does NOT touch `s.done`
        -- see `cursor_view` for the flag, and note that leaving the
        flag alone is what makes `advance` side-effect free.
        """
        done = set(done_ids or [])
        for s in steps:
            if s.step_id not in done:
                return s
        return None

    @staticmethod
    def cursor_view(steps: List[GuidedStep], done_ids, lang=None) -> Dict[str, Any]:
        """Return current step + progress for the frontend, marking done
        steps and refusing to skip ahead (cursor = first not-done).
        `lang` localizes all text fields via the fallback chain."""
        done = set(done_ids or [])
        current = TaskFlowEngine._current_step(steps, done_ids)
        for s in steps:
            if s.step_id in done:
                s.done = True
        remaining = 0
        if current is not None:
            remaining = sum(1 for s in steps if not s.done and s is not current) + 1
        return {
            "current": current.to_dict(lang) if current else None,
            "steps": [s.to_dict(lang) for s in steps],
            "remaining": remaining,
            "finished": current is None,
        }

    @staticmethod
    def advance(steps: List[GuidedStep], done_ids, step_id, skippable_ok=True):
        """Mark `step_id` done if and only if it is the current cursor
        step (or declared skippable) — the user cannot complete steps
        out of order silently."""
        current = TaskFlowEngine._current_step(steps, done_ids)
        if current is None:
            raise ValueError("flow already finished")
        if current.step_id == step_id:
            return done_ids + [step_id]
        # allow skipping a skippable current step explicitly
        target = next((s for s in steps if s.step_id == step_id), None)
        if target is not None and target.skippable and skippable_ok:
            return done_ids + [step_id]
        raise ValueError(
            f"step {step_id!r} is not the current step ({current.step_id!r}); "
            f"complete or explicitly skip the current step first")


FLOW_ENGINE = TaskFlowEngine()


def register_flow(flow: TaskFlow) -> TaskFlow:
    return FLOW_ENGINE.register(flow)
