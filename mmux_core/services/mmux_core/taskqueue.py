# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Task queue: the "get a task -> do it -> report done -> get the next one"
loop, server-driven.

This is the missing layer between the three existing building blocks:

  * **Capability Registry + ExecutionGuard** — what MAY be done (whitelist,
    risk policy, idempotency, audit);
  * **PlanOrchestrator** — how to chain several capabilities in one run;
  * **TaskFlow / TaskFlowEngine** — how to *navigate* one recipe step by
    step with voice.

What none of them provides is *which work item the operator should pick up
next*. That is this module.

Design (deliberately pure, no `odoo` import):

- A **TaskSource** is a code-defined adapter that says, for one scenario:
  which Odoo model holds the work items, which domain selects the *pending*
  ones for the current user, which field drives the priority ordering,
  which guided flow narrates the work, and which capability *completes*
  the task. Sources are registered per scenario by the scenario addons
  (warehouse -> stock.picking, manufacturing -> mrp.workorder, ...).
  This keeps the core generic and every scenario self-describing.

- A **Task** is one materialised work item: source + record id + a
  localized title + the resolved flow / completion capability + a numeric
  priority for stable ordering.

- The engine only ranks and shapes. The Odoo layer performs the actual
  `search`, because only it can honour access rules under the *current
  user's* identity (no privilege escalation anywhere in this module).

Loop state (what the frontend renders):

    idle                 no task fetched yet
    task_active          a task is being worked on
    awaiting_confirmation  the completion capability is L2 and is parked
                           waiting for the human's explicit confirmation
    finished_all         no pending task left for this user/scenario

Authority note: this module NEVER executes anything. `task_report` in the
Odoo layer routes the completion through the very same
`execute_capability` funnel, so guided/looped mode grants zero additional
authority (PRD FR-014/FR-015 unchanged).
"""
from typing import Any, Callable, Dict, List, Optional

from .i18n import resolve_text

# Ordered lifecycle of one task loop.
LOOP_STATES = ("idle", "task_active", "awaiting_confirmation", "finished_all")


class TaskSource:
    """Code-defined adapter: "which records in this scenario are tasks?".

    Every field is declarative; the Odoo backend turns `domain` /
    `priority_field` into a real `search`. `domain` may be a static list or
    a callable `(env, user) -> domain` when the filter depends on the user
    (e.g. "assigned to me").
    """

    __slots__ = ("source_id", "scenario", "task_model", "title_field",
                 "flow_id", "complete_capability", "priority_field",
                 "domain", "label", "description", "state_field",
                 "pending_states")

    def __init__(self, source_id, scenario, task_model, flow_id,
                 complete_capability, title_field="display_name",
                 priority_field="id", domain=None, label=None,
                 description="", state_field=None, pending_states=None):
        if not source_id:
            raise ValueError("TaskSource needs a source_id")
        if not task_model:
            raise ValueError(f"TaskSource {source_id}: task_model is required")
        if not flow_id:
            raise ValueError(f"TaskSource {source_id}: flow_id is required")
        if not complete_capability:
            raise ValueError(
                f"TaskSource {source_id}: complete_capability is required")
        self.source_id = source_id
        self.scenario = scenario
        self.task_model = task_model
        self.title_field = title_field
        self.flow_id = flow_id
        self.complete_capability = complete_capability
        self.priority_field = priority_field
        self.domain = domain if domain is not None else []
        self.label = label or source_id
        self.description = description
        # optional documentation of the native state machine; the concrete
        # domain is normally supplied by the scenario module instead.
        self.state_field = state_field
        self.pending_states = tuple(pending_states or ())

    def resolve_domain(self, env=None, user=None):
        """Static list, or call `domain(env, user)` when it is dynamic."""
        if callable(self.domain):
            return self.domain(env, user)
        return list(self.domain)

    def to_dict(self, lang=None):
        return {
            "source_id": self.source_id,
            "scenario": self.scenario,
            "task_model": self.task_model,
            "flow_id": self.flow_id,
            "complete_capability": self.complete_capability,
            "priority_field": self.priority_field,
            "label": resolve_text(self.label, lang),
            "description": resolve_text(self.description, lang),
        }


class Task:
    """One materialised work item handed to the operator."""

    __slots__ = ("task_id", "source_id", "scenario", "title", "res_model",
                 "res_id", "flow_id", "complete_capability", "priority",
                 "meta")

    def __init__(self, task_id, source_id, scenario, title, res_model,
                 res_id, flow_id, complete_capability, priority=0, meta=None):
        self.task_id = task_id            # stable id, e.g. "wh_picking:42"
        self.source_id = source_id
        self.scenario = scenario
        self.title = title                # plain str or {lang: text}
        self.res_model = res_model
        self.res_id = res_id
        self.flow_id = flow_id
        self.complete_capability = complete_capability
        # lower priority value == picked earlier (mirrors Odoo's
        # `priority` semantics being normalised by the backend)
        self.priority = priority
        self.meta = dict(meta or {})

    @property
    def key(self):
        return self.task_id

    def to_dict(self, lang=None):
        return {
            "task_id": self.task_id,
            "source_id": self.source_id,
            "scenario": self.scenario,
            "title": resolve_text(self.title, lang),
            "res_model": self.res_model,
            "res_id": self.res_id,
            "flow_id": self.flow_id,
            "complete_capability": self.complete_capability,
            "priority": self.priority,
            "meta": self.meta,
        }


class TaskQueueEngine:
    """Registry of task sources + the deterministic next-task selector."""

    def __init__(self):
        self._sources: Dict[str, TaskSource] = {}
        self._order: List[str] = []

    # ---------------------------------------------------------------- #
    # registry
    # ---------------------------------------------------------------- #
    def register(self, source: TaskSource) -> TaskSource:
        if source.source_id in self._sources:
            raise ValueError(f"duplicate task source id {source.source_id}")
        self._sources[source.source_id] = source
        self._order.append(source.source_id)   # registration order matters
        return source

    def get(self, source_id) -> Optional[TaskSource]:
        return self._sources.get(source_id)

    def all(self) -> List[TaskSource]:
        return [self._sources[sid] for sid in self._order]

    def for_scenario(self, scenario="") -> List[TaskSource]:
        return [s for s in self.all()
                if not scenario or not s.scenario or s.scenario == scenario]

    def rank(self, source_id) -> int:
        """Registration index — the tie-breaker when priorities are equal."""
        try:
            return self._order.index(source_id)
        except ValueError:
            return len(self._order)

    # ---------------------------------------------------------------- #
    # selection
    # ---------------------------------------------------------------- #
    def next_task(self, candidates: List[Task],
                  only_source: str = None) -> Optional[Task]:
        """The next task to publish.

        Deterministic and explainable:
          1. lowest `priority` value first (urgent / earliest deadline),
          2. then the order in which the source was registered,
          3. then the task id (stable, so a refresh never flip-flops).

        `only_source` restricts the choice to one source (e.g. "keep me on
        pickings"). Returns None when nothing is pending.
        """
        pool = [t for t in (candidates or [])
                if not only_source or t.source_id == only_source]
        if not pool:
            return None
        return min(pool, key=lambda t: (t.priority,
                                        self.rank(t.source_id),
                                        str(t.task_id)))


TASK_ENGINE = TaskQueueEngine()


def register_task_source(source: TaskSource) -> TaskSource:
    return TASK_ENGINE.register(source)


def next_task(candidates: List[Task], only_source: str = None,
              engine: TaskQueueEngine = None) -> Optional[Task]:
    """Module-level shortcut for `engine.next_task(candidates, only_source)`."""
    return (engine or TASK_ENGINE).next_task(candidates, only_source)


# --------------------------------------------------------------------- #
# loop state
# --------------------------------------------------------------------- #
def build_loop_state(current: Optional[Task] = None,
                     next_task: Optional[Task] = None,
                     done_count: int = 0,
                     awaiting_confirmation: bool = False) -> Dict[str, Any]:
    """Describe where the loop stands, for the frontend to render.

    The state machine is intentionally tiny and explicit:
      idle                 -> nothing fetched yet
      awaiting_confirmation-> a task is parked on an L2 confirmation
      task_active          -> there IS a task to work on
      finished_all         -> nothing current AND nothing next
    """
    if awaiting_confirmation and current is not None:
        state = "awaiting_confirmation"
    elif current is not None:
        state = "task_active"
    elif next_task is None:
        state = "finished_all"
    else:
        state = "idle"
    return {
        "state": state,
        "done_count": int(done_count or 0),
        "has_current": current is not None,
        "has_next": next_task is not None,
        "current_task_id": current.task_id if current else None,
        "next_task_id": next_task.task_id if next_task else None,
    }


def loop_label(state, lang=None):
    """Human-readable loop state (used by the UI and the audit log)."""
    labels = {
        "idle": {"zh_CN": "等待获取任务", "zh": "等待获取任务",
                 "en_US": "Waiting for a task", "en": "Waiting for a task"},
        "task_active": {"zh_CN": "任务进行中", "zh": "任务进行中",
                        "en_US": "Task in progress", "en": "Task in progress"},
        "awaiting_confirmation": {
            "zh_CN": "等待你确认后过账", "zh": "等待你确认后过账",
            "en_US": "Awaiting your confirmation",
            "en": "Awaiting your confirmation"},
        "finished_all": {"zh_CN": "当前没有待办任务", "zh": "当前没有待办任务",
                         "en_US": "No pending tasks", "en": "No pending tasks"},
    }
    return resolve_text(labels.get(state, state), lang)
