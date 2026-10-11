# -*- coding: utf-8 -*-
"""Tasks and guided flows: the task-selection engine, the task loop and
the scripted guides.  Calls into the loop cluster one-way (guide_start
-> agent_start) and never the reverse.
"""
import json
import logging

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

from odoo import api, fields, models

from ..services.mmux_core import (
    CAPABILITY_REGISTRY,
    ExecutionGuard, ExecStatus, GuardError,
    IntentParser, NORMALIZED_LANG,
    PROVIDER_REGISTRY,
    ProviderError,
    Plan, PlanStep, PlanOrchestrator, PlanError,
    PLAN_LIBRARY, validate_llm_plan,
    TaskFlowEngine, FLOW_ENGINE,
    Task, TASK_ENGINE, build_loop_state, loop_label,
    AgentPick, validate_pick, fallback_pick,
    SOURCE_LLM, SOURCE_FALLBACK, SOURCE_EMPTY,
    AgentAction, AgentObservation, AgentScratchpad, AgentTurn,
    validate_action, build_loop_prompt, step_from_action,
    execution_request, run_loop,
    AGENT_ACTIONS, LOOP_STATES,
    LOOP_SOURCE_LLM, LOOP_SOURCE_RECIPE, LOOP_SOURCE_NONE,
    MAX_AGENT_TURNS, MAX_AGENT_CAPABILITY_CALLS,
    ContextSignal, ScenarioResolution, validate_scenario_choice,
    choice_from_answer, resolve_scenario, build_scenario_prompt,
    build_ask_question,
    unresolved_with_all,
    SOURCE_SIGNAL, SOURCE_AMBIGUOUS, SOURCE_NONE, SOURCE_USER,
    CHOICE_SOURCE_LLM,
    idempotency_key,
    normalize_lang, resolve_text, detect_languages,
    mixed_language_hint,
)


class MmuxService(models.AbstractModel):
    _inherit = "mmux.service"

    # -------------------------------------------------------------- #
    # guide_flows
    # -------------------------------------------------------------- #
    # ================================================================== #
    # Guided task flows: voice navigation (warehouse picking etc.)
    # ================================================================== #
    @api.model
    def _flow_for(self, flow_id):
        """Resolve a flow: DB override row first, code registration second.

        The single funnel every flow lookup goes through (v19.0.6), so
        the override precedence cannot differ between the guide menu,
        the guided run and the agent observation. Returns None when
        neither source has the id — callers raise their own error, the
        same shape `FLOW_ENGINE.build_steps` used to raise, so existing
        `except KeyError` branches keep working.
        """
        # no sudo: mmux.flow.override is readable by group_mmux_user by
        # ACL on purpose — following an override is just following
        # steps; the guard governs what they may do.
        row = self.env["mmux.flow.override"].search(
            [("flow_id", "=", flow_id)], limit=1)
        if row:
            return row.to_taskflow()
        return FLOW_ENGINE.get(flow_id)

    @api.model
    def guide_flows(self):
        """Available guided flows (frontend menu for the guide mode).
        Also reports whether each flow differentiates skill levels and the
        current user's saved skill level.

        Code flows are the base list; DB override rows replace same-id
        entries (and may add flows that exist only as data). Sorted by
        flow_id so the menu order cannot jitter between requests."""
        skill = self._user_skill_level()
        lang = self._user_lang()
        flows = {f.flow_id: f for f in FLOW_ENGINE.all()}
        for row in self.env["mmux.flow.override"].search([]):
            flows[row.flow_id] = row.to_taskflow()
        return [{"flow_id": f.flow_id, "name": resolve_text(f.name, lang),
                 "description": resolve_text(f.description, lang),
                 "supports_skill_levels": f.supports_skill_levels,
                 "skill_level": skill, "lang": lang}
                for f in sorted(flows.values(), key=lambda f: f.flow_id)]

    # -------------------------------------------------------------- #
    # guide_start
    # -------------------------------------------------------------- #
    @api.model
    def guide_start(self, flow_id, params=None, mode="scripted"):
        """Start a guided run. Builds the step list and returns the first
        voice prompt. The run cursor is stored in the interaction log so
        navigation survives page switches (PRD §9.3 states 1-2).

        `mode` selects who drives the navigation (v3.0):

        * ``"scripted"`` (default) — the v2.x behaviour: the server walks
          the flow's steps in order. Kept as the default so existing
          callers and tests are unaffected, and as a genuine fallback.
        * ``"agent"`` — the same flow is handed to the agent loop, which
          narrates its own way through the work and degrades to the flow's
          recipe if the model is unavailable. The FlowEngine becomes the
          *source of the recipe*, not the driver.
        """
        if mode == "agent":
            return self.agent_start(loop={"flow_id": flow_id,
                                          "params": params or {},
                                          "goal": flow_id})
        skill = self._user_skill_level()
        try:
            steps = self._guide_steps(flow_id, params or {}, skill)
        except KeyError as exc:
            return {"error": str(exc)}
        key = idempotency_key("guide")
        run = {"flow_id": flow_id, "params": params or {},
               "done_ids": [], "step_count": len(steps),
               "skill_level": skill}
        self._guide_save(key, run)
        lang = self._user_lang()
        view = TaskFlowEngine.cursor_view(steps, run["done_ids"], lang=lang)
        view.update({"guide_key": key, "flow_id": flow_id,
                     "skill_level": skill, "lang": lang})
        return view

    # -------------------------------------------------------------- #
    # _guide_steps
    # -------------------------------------------------------------- #
    @api.model
    def _guide_steps(self, flow_id, params, skill_level):
        """Build a guide's step list once per request.

        Why this exists
        ---------------
        The builders behind a flow read LIVE business records — a
        picking's `move_line_ids`, a workorder's product, a lead's
        partner.  Every `guide_*` call used to rebuild the whole list
        from the database: one `build_steps()` (N queries plus a
        `display_name` per move line) and then THREE full serialisations
        of every step, because `advance()` used to reach `cursor_view()`
        internally just to read the current step's id.

        Scope: `self.env`, not `env.registry`.  A step list is built from
        records this user may see and is narrowed by `env.user.id` — one
        worker-wide copy would leak one operator's work into another's
        screen.  An env lasts exactly one request, which is the lifetime
        we want.  (The registry-level caches next door — `_parser`,
        `_orchestrator`, `_caps_payload` — are safe there because what
        they hold is code-derived and carries no user data at all.)

        Invalidation is the caller's job and is deliberately explicit:
        `guide_advance` drops the entry after a capability WRITES, since
        a write can change the very records the builder reads (a
        validated picking can gain a backorder line).  Caching across
        that would show the operator a step list describing a picking
        that no longer exists — and no existing test would catch it,
        because none of them reloads the guide after a write.

        `GuidedStep` is mutable — `cursor_view` sets `s.done` — so the
        flag is reset on the way out.  That keeps the cached objects
        clean no matter how many times, or in what order, a caller
        renders them.
        """
        key = ("guide_steps", flow_id,
               json.dumps(params or {}, sort_keys=True, ensure_ascii=False),
               skill_level)
        cache = getattr(self.env, "_mmux_guide_steps", None)
        if cache is None:
            cache = {}
            self.env._mmux_guide_steps = cache
        steps = cache.get(key)
        if steps is None:
            flow = self._flow_for(flow_id)
            if flow is None:
                # same message `FLOW_ENGINE.build_steps` raised, so the
                # caller's `except KeyError` branch is unchanged.
                raise KeyError(
                    f"flow {flow_id!r} is not registered")
            steps = TaskFlowEngine.build_steps_for(flow, self.env, params,
                                                   skill_level=skill_level)
            cache[key] = steps
        for s in steps:
            s.done = False
        return steps

    # -------------------------------------------------------------- #
    # _guide_invalidate_steps
    # -------------------------------------------------------------- #
    @api.model
    def _guide_invalidate_steps(self):
        """Forget the request's step lists (called after a write).

        A whole-cache drop rather than key-by-key: the builder may read
        records through relations (a move's picking, a line's product),
        so reasoning about which keys a given write invalidated is
        guesswork.  Within one request there is at most a handful of
        entries, and correctness is worth more than the rebuild.
        """
        self.env._mmux_guide_steps = {}

    # -------------------------------------------------------------- #
    # guide_current
    # -------------------------------------------------------------- #
    @api.model
    def guide_current(self, guide_key):
        run = self._guide_load(guide_key)
        if run is None:
            return {"error": _("unknown guide run (expired?) — restart the flow")}
        skill = run.get("skill_level") or self._user_skill_level()
        steps = self._guide_steps(run["flow_id"], run["params"], skill)
        lang = self._user_lang()
        view = TaskFlowEngine.cursor_view(steps, run["done_ids"], lang=lang)
        view.update({"guide_key": guide_key, "flow_id": run["flow_id"],
                     "skill_level": skill, "lang": lang})
        return view

    # -------------------------------------------------------------- #
    # guide_advance
    # -------------------------------------------------------------- #
    @api.model
    def guide_advance(self, guide_key, step_id, input_data=None,
                      skip=False):
        """Complete the current guided step.

        If the step declares a capability, the step's input is executed
        through the SAME execute_capability funnel (schema/ACL/state/
        risk/idempotency); guided mode grants zero extra authority. Only
        after a success (or an explicit skip of a skippable step) does
        the cursor advance and the next voice prompt come back."""
        run = self._guide_load(guide_key)
        if run is None:
            return {"error": _("unknown guide run — restart the flow")}
        skill = run.get("skill_level") or self._user_skill_level()
        steps = self._guide_steps(run["flow_id"], run["params"], skill)

        # Only the CURRENT step is serialised here.  The old code called
        # `cursor_view` for this, paying to serialise every step (titles,
        # prompts, hints, in the user's language) in order to read one
        # step_id -- and `advance` then did it a second time internally.
        current_step = TaskFlowEngine._current_step(steps, run["done_ids"])
        if current_step is None:
            return {"state": "finished",
                    "steps": [s.to_dict() for s in steps]}
        current = current_step.to_dict()
        if current["step_id"] != step_id and not (skip and current["skippable"]):
            return {"error": _(
                "step %s is not the current step (%s)") % (step_id, current["step_id"])}

        exec_status = None
        if current.get("capability_id") and not skip:
            res = self.execute_capability(
                current["capability_id"],
                dict(current.get("params") or {}, **(input_data or {})),
                record_id=(input_data or {}).get("record_id")
                or current.get("record_ref"),
                idem_key=f"{guide_key}:{step_id}",
                confirmed=bool((input_data or {}).get("confirmed", False)),
            )
            exec_status = res.get("status")
            if exec_status == "awaiting_confirmation":
                # surface the confirmation and stay on this step
                return {"state": "awaiting_confirmation",
                        "confirmation": res.get("confirmation"),
                        "step": current}
            if exec_status not in ("success",):
                return {"state": "step_failed", "step": current,
                        "exec": res}
            # A capability that succeeded may have written records the
            # builder reads (validating a picking can add a backorder
            # line), so the list we are holding may no longer describe
            # the work in front of the operator.  Drop it: the view
            # below re-reads reality.  This is the one place a guide run
            # can change the ground truth mid-request.
            self._guide_invalidate_steps()

        # advance cursor (skip allowed only on skippable steps)
        try:
            run["done_ids"] = TaskFlowEngine.advance(
                steps, run["done_ids"], step_id,
                skippable_ok=bool(skip or current.get("skippable")))
        except ValueError as exc:
            return {"error": str(exc)}
        self._guide_save(guide_key, run)
        lang = self._user_lang()
        # Re-read: if a capability wrote above, the cache was dropped and
        # this rebuilds from the records as they now stand, so the
        # operator's next prompt describes the work that is actually
        # left.  Without a write this is the same cached list the cursor
        # was computed from -- so the common path pays for ONE build and
        # ONE full serialisation per advance.
        steps = self._guide_steps(run["flow_id"], run["params"], skill)
        view = TaskFlowEngine.cursor_view(steps, run["done_ids"], lang=lang)
        view.update({"guide_key": guide_key, "flow_id": run["flow_id"],
                     "skill_level": skill, "lang": lang,
                     "last_exec": exec_status})
        return view

    # -------------------------------------------------------------- #
    # _guide_save
    # -------------------------------------------------------------- #
    # ---- cursor persistence: single log row per guide run ------------ #
    @api.model
    def _guide_save(self, key, run):
        Log = self.env["mmux.interaction.log"].sudo()
        existing = Log.search([("idempotency_key", "=", key)], limit=1)
        detail = json.dumps(run, ensure_ascii=False)
        if existing:
            existing.write({"detail": detail, "exec_status": "running",
                            "capability_id_name": f"guide:{run['flow_id']}"})
        else:
            Log.create({"idempotency_key": key, "user_id": self.env.user.id,
                        "input_type": "combo", "exec_status": "running",
                        "capability_id_name": f"guide:{run['flow_id']}",
                        "detail": detail})

    # -------------------------------------------------------------- #
    # _guide_load
    # -------------------------------------------------------------- #
    @api.model
    def _guide_load(self, key):
        Log = self.env["mmux.interaction.log"].sudo()
        rec = Log.search([("idempotency_key", "=", key),
                          ("user_id", "=", self.env.user.id)], limit=1)
        if not rec or not rec.detail:
            return None
        try:
            return json.loads(rec.detail)
        except ValueError:
            return None

    # -------------------------------------------------------------- #
    # task_sources
    # -------------------------------------------------------------- #
    # ================================================================== #
    # Task loop: get a task -> do it -> report done -> get the next one
    # ================================================================== #
    @api.model
    def task_sources(self, scenario=""):
        """List the registered task sources (what counts as a task).

        Sources are code-defined by the scenario addons; this only
        reports them so the UI (and admins) can see the contract.
        """
        lang = self._user_lang()
        return [s.to_dict(lang) for s in TASK_ENGINE.for_scenario(scenario)]

    # -------------------------------------------------------------- #
    # _materialize
    # -------------------------------------------------------------- #
    @api.model
    def _materialize(self, source, record):
        """Turn one Odoo record into a Task, using the source's declared
        priority field for ordering. Priority is whatever the native
        ordering field yields (lower first), so urgent work surfaces."""
        prio = record.id
        field = (source.priority_field or "").split(",")[0].strip()
        if field:
            value = record[field] if field in record._fields else None
            if isinstance(value, (int, float)):
                prio = value
            elif value:  # datetime / date / char -> sortable string
                prio = str(value)
            else:
                prio = record.id
        title_field = source.title_field
        title = (record[title_field]
                 if title_field in record._fields else record.display_name)
        if not isinstance(title, str):
            title = record.display_name
        return Task(
            task_id=f"{source.source_id}:{record.id}",
            source_id=source.source_id,
            scenario=source.scenario,
            title=title,
            res_model=source.task_model,
            res_id=record.id,
            flow_id=source.flow_id,
            complete_capability=source.complete_capability,
            priority=prio,
            meta={"display_name": record.display_name},
        )

    # -------------------------------------------------------------- #
    # _candidates
    # -------------------------------------------------------------- #
    @api.model
    def _candidates(self, source):
        """Pending tasks for one source, under the CURRENT user's rights.

        No `sudo()` here on purpose: whatever the source's domain cannot
        see is genuinely not this user's work (PRD §6.2 — no privilege
        escalation in the request path).
        """
        Model = self.env[source.task_model]
        domain = source.resolve_domain(self.env, self.env.user)
        records = Model.search(domain, order=source.priority_field or "id",
                               limit=50)
        return [self._materialize(source, rec) for rec in records]

    # -------------------------------------------------------------- #
    # _select_task
    # -------------------------------------------------------------- #
    @api.model
    def _select_task(self, scenario="", source_id=None, res_id=None,
                     use_agent=True):
        """Pick the next task.

        Two paths, deliberately:

        * `res_id` given -> *restore that exact record* (page refresh /
          resume). No agent is consulted: the user has already told us
          which task they mean, so there is nothing to decide.
        * otherwise -> the **agent** chooses from the candidate pool; if
          it is unavailable or its answer is rejected, the plain priority
          queue decides (see `_agent_pick`).

        The return contract `(chosen, source_id, sources)` is unchanged;
        the decision itself is stashed on `self._last_pick` so callers can
        surface *why* without the shape of this method moving under the
        four scenario modules and the existing tests.
        """
        self._last_pick = None
        sources = TASK_ENGINE.for_scenario(scenario)
        if source_id:
            only = TASK_ENGINE.get(source_id)
            sources = [only] if only else []
        if not sources:
            return None, None, []
        pool = []
        for source in sources:
            pool.extend(self._candidates(source))
        if res_id:
            # refresh/resume one exact record
            for t in pool:
                if t.res_id == int(res_id) and (
                        not source_id or t.source_id == source_id):
                    return t, source_id, sources
            # the record may no longer be pending (done meanwhile) — build a
            # transient descriptor so the caller still gets a clear answer.
            return None, source_id or (sources[0].source_id if sources else None), sources

        pick = None
        if use_agent and pool:
            pick = self._agent_pick(pool, scenario, source_id)
        if pick is not None and pick.is_valid:
            chosen = next((t for t in pool if t.task_id == pick.task_id), None)
            if chosen is not None:
                self._last_pick = pick
                return chosen, source_id, sources
            # unreachable in practice (validate_pick guarantees membership)
            # — fall through to the queue rather than trust the pick.
        # deterministic fallback: the loop must never fail because the
        # model is down.
        self._last_pick = fallback_pick(pool, only_source=source_id)
        chosen = TASK_ENGINE.next_task(pool, only_source=source_id)
        return chosen, source_id, sources

    # -------------------------------------------------------------- #
    # _serialize_candidates
    # -------------------------------------------------------------- #
    # ------------------------------------------------------------------ #
    # the agent: "which task should I do NOW?" (LLM, strictly bounded)
    # ------------------------------------------------------------------ #
    @api.model
    def _serialize_candidates(self, pool, lang=None):
        """Trim the pool to the **minimum** fields a decision needs.

        Data minimisation is deliberate: the model is shown an id, a
        short title, a source, a priority and a due date — never the full
        record, customer names, costs, suppliers or internal notes. What
        it is not shown, it cannot leak into a prompt, a log or a UI.
        """
        out = []
        for t in pool:
            meta = t.meta or {}
            due = meta.get("due") or meta.get("scheduled_date") \
                or meta.get("date_deadline")
            out.append({
                "task_id": t.task_id,
                "source_id": t.source_id,
                "scenario": t.scenario,
                "title": resolve_text(t.title, lang or NORMALIZED_LANG),
                "priority": t.priority,
                "due": str(due) if due else None,
                "kind": t.res_model,
            })
        return out

    # -------------------------------------------------------------- #
    # _build_pick_prompt
    # -------------------------------------------------------------- #
    @api.model
    def _build_pick_prompt(self, candidates, scenario=""):
        """Build the pick prompt. The wording is the first line of defence:
        it states plainly that the model may only *choose*, never act."""
        sys = (
            "You are an operations agent embedded in Odoo. You are given a "
            "list of the tasks currently assigned to ONE operator, already "
            "filtered by the server to records this operator is allowed to "
            "see. Decide which SINGLE task the operator should work on now, "
            "and explain the choice briefly.\n"
            "Rules:\n"
            "- You may ONLY return a task_id that appears verbatim in the "
            "given list. Never invent, guess or modify an id.\n"
            "- Prefer the most urgent, most time-critical, highest-impact "
            "work (overdue before due-today; blocking others before "
            "self-contained; high-value customer before routine).\n"
            "- You only SELECT. You never execute, confirm, write or call "
            "anything; a separate, human-confirmed step performs the work.\n"
            "- Respond ONLY with JSON: "
            '{"selected_task_id": "<id from the list>", '
            '"reason": {"zh_CN": "...", "en_US": "..."}, '
            '"confidence": 0.0-1.0, '
            '"alternatives": [{"task_id": "<id from the list>", '
            '"why_not": {"zh_CN": "...", "en_US": "..."}}]}'
        )
        user = json.dumps(
            {"scenario": scenario, "candidates": candidates},
            ensure_ascii=False)
        return [{"role": "system", "content": sys},
                {"role": "user", "content": user}]

    # -------------------------------------------------------------- #
    # _agent_pick
    # -------------------------------------------------------------- #
    @api.model
    def _agent_pick(self, pool, scenario="", only_source=None):
        """The ONLY place the LLM is asked to make a task decision.

        Any failure — no provider, timeout, invalid JSON, or an answer
        that fails validation — returns an invalid `AgentPick`, which the
        caller turns into a plain queue pick. This asymmetry is on
        purpose: `parse_intent` may report `llm_unavailable` upward, but
        **fetching a task must never fail because the model is down.**

        The candidate list passed to `validate_pick` is the *server's*
        pool, never anything that came back from the client — that is what
        makes membership checking meaningful.
        """
        config = self._provider_config()
        llm_provider = config.get("mmux.llm.provider")
        if not llm_provider:
            return AgentPick(rejected_reason="no_provider")
        if llm_provider != "mock" and not config.get("mmux.llm.api_key"):
            return AgentPick(rejected_reason="no_api_key")

        lang = self._user_lang()
        candidates = self._serialize_candidates(pool, lang)
        prompt = self._build_pick_prompt(candidates, scenario)
        try:
            raw = PROVIDER_REGISTRY.call("llm", llm_provider,
                                         {"messages": prompt}, config)
        except ProviderError as exc:
            _logger.info("agent task pick failed: %s", exc)
            return AgentPick(rejected_reason="provider_error")
        except Exception as exc:                       # defensive
            _logger.info("agent task pick unexpected error: %s", exc)
            return AgentPick(rejected_reason="provider_error")

        data = raw.get("data") if isinstance(raw, dict) else None
        if data is None and isinstance(raw, dict) and isinstance(raw.get("raw"), str):
            try:
                data = json.loads(raw["raw"])
            except (json.JSONDecodeError, ValueError):
                return AgentPick(rejected_reason="invalid_json")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                return AgentPick(rejected_reason="invalid_json")

        pick = validate_pick(data, pool, only_source=only_source)
        self._log_agent_pick(pick, pool, scenario)
        return pick

    # -------------------------------------------------------------- #
    # task_next
    # -------------------------------------------------------------- #
    @api.model
    def task_next(self, scenario="", source_id=None, res_id=None):
        """Publish the next task for the current user (server-driven loop).

        Returns `{loop, task, sources}`. When nothing is pending the loop
        state is `finished_all` and `task` is null — that is a normal end
        of the loop, not an error.
        """
        chosen, _only, sources = self._select_task(scenario, source_id, res_id)
        lang = self._user_lang()
        pick = self._last_pick or AgentPick(rejected_reason="no_candidates")
        if chosen is None:
            return {"task": None,
                    "loop": build_loop_state(None, None, 0),
                    "loop_label": loop_label("finished_all", lang),
                    "sources": [s.to_dict(lang) for s in sources],
                    "agent": pick.to_dict(lang)}
        key = idempotency_key("task")
        run = {"scenario": chosen.scenario,
               "source_id": chosen.source_id,
               "task_id": chosen.task_id,
               "res_model": chosen.res_model,
               "res_id": chosen.res_id,
               "flow_id": chosen.flow_id,
               "capability_id": chosen.complete_capability,
               "title": chosen.title,
               "done_count": 0,
               "guide_key": None,
               "agent": pick.to_dict(lang)}
        self._loop_save(key, run)
        return {
            "task": chosen.to_dict(lang),
            "loop": build_loop_state(chosen, None, 0),
            "loop_key": key,
            "loop_label": loop_label("task_active", lang),
            "sources": [s.to_dict(lang) for s in sources],
            "agent": pick.to_dict(lang),
        }

    # -------------------------------------------------------------- #
    # task_current
    # -------------------------------------------------------------- #
    @api.model
    def task_current(self, loop_key):
        """Resume the loop (page switch / reload) — cursor persisted in
        the interaction log, exactly like the guided-flow cursor."""
        run = self._loop_load(loop_key)
        if run is None:
            return {"error": _("unknown task loop (expired?) — fetch again")}
        lang = self._user_lang()
        task = Task(run["task_id"], run["source_id"], run["scenario"],
                    run["title"], run["res_model"], run["res_id"],
                    run["flow_id"], run["capability_id"])
        return {"task": task.to_dict(lang),
                "loop": build_loop_state(task, None, run.get("done_count", 0)),
                "loop_key": loop_key,
                "loop_label": loop_label("task_active", lang),
                "guide_key": run.get("guide_key"),
                "agent": run.get("agent")}

    # -------------------------------------------------------------- #
    # task_guide
    # -------------------------------------------------------------- #
    @api.model
    def task_guide(self, loop_key, mode=None):
        """Start (or resume) the guided navigation bound to the current
        task, so "execute the task" and "ask the system for guidance" are
        the same mechanism (and the same guard).

        `mode` (v3.0):

        * unset / ``"scripted"`` — the v2.x scripted flow, unchanged.
        * ``"agent"`` — the agent loop drives the same recipe: it decides
          what to narrate next and which capability to propose, under the
          full guard. This is the "pure agent" path; the scripted path is
          kept because it is also the agent's own fallback.

        When the agent is enabled and no explicit mode is given, we pick
        the agent path — that IS the architecture switch — but any error
        in setting it up degrades to the scripted flow rather than leaving
        the operator with nothing.
        """
        run = self._loop_load(loop_key)
        if run is None:
            return {"error": _("unknown task loop — fetch a task first")}
        if mode is None:
            mode = "agent" if self._agent_enabled() else "scripted"
        if mode == "agent":
            view = self.agent_start(loop_key=loop_key)
            if view.get("error"):
                _logger.info("agent loop unavailable, using scripted guide: %s",
                             view.get("error"))
                mode = "scripted"
            else:
                view["mode"] = "agent"
                view["task_id"] = run.get("task_id")
                return view

        params = {"res_id": run["res_id"]}
        model = run["res_model"]
        # each flow names its own anchor param; keep both common spellings
        if model == "stock.picking":
            params["picking_id"] = run["res_id"]
        elif model == "mrp.workorder":
            params["workorder_id"] = run["res_id"]
        elif model == "project.task":
            params["task_id"] = run["res_id"]
        elif model == "crm.lead":
            params["lead_id"] = run["res_id"]
        view = self.guide_start(run["flow_id"], params)
        if view.get("error"):
            return view
        run["guide_key"] = view.get("guide_key")
        self._loop_save(loop_key, run)
        view["loop_key"] = loop_key
        view["task_id"] = run["task_id"]
        view["mode"] = "scripted"
        return view

    # -------------------------------------------------------------- #
    # task_help
    # -------------------------------------------------------------- #
    @api.model
    def task_help(self, loop_key, text=None, intent=None):
        """Answer a mid-task question ("库位在哪 / 数量填多少 / how many?").

        Two outcomes, deliberately distinguished:
        - the LLM resolves it to a *capability* call -> `action: "execute"`
          (the caller may then run it through the normal funnel);
        - anything else -> `action: "guidance"`: we return the recognized
          canonical text and the current step, i.e. help, never a silent
          business write. Mixed-language input is fine: the LLM
          normalises it to one pure language first.
        """
        run = self._loop_load(loop_key)
        if run is None:
            return {"error": _("unknown task loop")}
        lang = self._user_lang()
        if not text:
            return {"action": "guidance", "answer_type": "noop",
                    "hint": loop_label("task_active", lang)}
        context = {"scenario": run["scenario"],
                   "task_id": run["task_id"],
                   "flow_id": run["flow_id"]}
        parsed = intent or self.parse_intent(text, context)
        if parsed.get("code") == LLM_UNAVAILABLE:
            return parsed  # structured llm_unavailable error, verbatim
        capability_id = parsed.get("capability_id")
        if capability_id:
            return {"action": "execute", "intent": parsed,
                    "hint": parsed.get("normalized_text") or text}
        return {"action": "guidance", "answer_type": "semantic",
                "normalized_text": parsed.get("normalized_text"),
                "missing": parsed.get("missing") or [],
                "ambiguous": parsed.get("ambiguous") or [],
                "hint": parsed.get("normalized_text") or text}

    # -------------------------------------------------------------- #
    # task_report
    # -------------------------------------------------------------- #
    @api.model
    def task_report(self, loop_key, text=None, intent=None, params=None,
                    confirmed=False):
        """Report a task complete: mark it done and publish the next one.

        The report is an ordinary intent: unless the caller already supplies
        a validated `intent`, the free text goes through the LLM-first path
        (`parse_intent`), which normalises however many languages were mixed
        into ONE canonical language and returns a whitelisted capability +
        params. We then require that capability to be *the task's own
        completion capability* — a report must not quietly become some other
        business action.

        The execution itself goes through `execute_capability`, i.e. the
        full ExecutionGuard. An L2 completion capability therefore parks on
        `awaiting_confirmation` and the loop does NOT advance until the
        human confirms.
        """
        run = self._loop_load(loop_key)
        if run is None:
            return {"error": _("unknown task loop — fetch a task first")}

        lang = self._user_lang()
        expected = run["capability_id"]

        # 1) recognise the report (LLM-first, canonical language)
        if intent is None:
            if not text or not str(text).strip():
                return {"error": _("empty completion report")}
            parsed = self.parse_intent(text, {
                "scenario": run["scenario"],
                "task_id": run["task_id"],
                "expected_capability": expected,
                "res_model": run["res_model"],
                "res_id": run["res_id"],
            })
            if parsed.get("code") == LLM_UNAVAILABLE:
                return parsed  # structured error; the client retries
            intent = parsed

        capability_id = intent.get("capability_id")
        if not capability_id:
            return {"state": "not_understood",
                    "error": _("the report did not map to any action; "
                               "please rephrase or complete the steps"),
                    "normalized_text": intent.get("normalized_text"),
                    "missing": intent.get("missing") or [],
                    "ambiguous": intent.get("ambiguous") or []}
        if capability_id != expected:
            return {"state": "task_mismatch",
                    "error": _("that action (%(got)s) is not how this task "
                               "is completed (%(want)s)")
                    % {"got": capability_id, "want": expected},
                    "expected_capability": expected}

        merged = dict(intent.get("params") or {}, **(params or {}))
        res = self.execute_capability(
            expected, merged, record_id=run["res_id"],
            idem_key=f"{loop_key}:complete", confirmed=confirmed)

        status = res.get("status")
        if status == "awaiting_confirmation":
            # park the loop; cursor (and task) stay put until confirmed
            return {"state": "awaiting_confirmation",
                    "confirmation": res.get("confirmation"),
                    "exec": res,
                    "loop": build_loop_state(
                        self._task_from_run(run), None,
                        run.get("done_count", 0), awaiting_confirmation=True),
                    "loop_label": loop_label("awaiting_confirmation", lang),
                    "loop_key": loop_key}
        if status != "success":
            return {"state": "report_failed", "exec": res,
                    "loop": build_loop_state(self._task_from_run(run), None,
                                             run.get("done_count", 0)),
                    "loop_key": loop_key}

        # 2) mark done + publish the next task (the loop closes and reopens)
        run["done_count"] = int(run.get("done_count", 0)) + 1
        run["completed"] = run.get("completed", [])
        run["completed"].append({"task_id": run["task_id"],
                                 "at": fields.Datetime.now().isoformat(),
                                 "exec_status": status})
        self._loop_save(loop_key, run, status="success")

        nxt, _only, _sources = self._select_task(
            run["scenario"], run["source_id"])
        pick = self._last_pick or AgentPick(rejected_reason="no_candidates")
        # never hand back the task we just finished
        if nxt is not None and nxt.task_id == run["task_id"]:
            pool = [t for t in self._candidates(TASK_ENGINE.get(run["source_id"]))
                    if t.task_id != run["task_id"]]
            if pool:
                # re-ask the agent WITHOUT the just-finished task, so its
                # reason still describes a choice it actually made.
                pick = self._agent_pick(pool, run["scenario"], run["source_id"])
                if not pick.is_valid:
                    pick = fallback_pick(pool, only_source=run["source_id"])
                nxt = next((t for t in pool if t.task_id == pick.task_id), None) \
                    or TASK_ENGINE.next_task(pool)
            else:
                nxt = None

        out = {"state": "completed", "exec": res, "loop_key": loop_key,
               "done_count": run["done_count"],
               "completed_task_id": run["task_id"],
               "agent": pick.to_dict(lang)}
        if nxt is None:
            out.update({"task": None, "next_task": None,
                        "loop": build_loop_state(None, None, run["done_count"]),
                        "loop_label": loop_label("finished_all", lang)})
            return out
        next_key = idempotency_key("task")
        self._loop_save(next_key, {
            "scenario": nxt.scenario, "source_id": nxt.source_id,
            "task_id": nxt.task_id, "res_model": nxt.res_model,
            "res_id": nxt.res_id, "flow_id": nxt.flow_id,
            "capability_id": nxt.complete_capability, "title": nxt.title,
            "done_count": run["done_count"], "guide_key": None,
            "agent": pick.to_dict(lang)})
        out.update({"task": nxt.to_dict(lang), "next_task": nxt.to_dict(lang),
                    "next_loop_key": next_key,
                    "loop": build_loop_state(nxt, None, run["done_count"]),
                    "loop_label": loop_label("task_active", lang)})
        return out

    # -------------------------------------------------------------- #
    # _task_from_run
    # -------------------------------------------------------------- #
    @api.model
    def _task_from_run(self, run):
        return Task(run["task_id"], run["source_id"], run["scenario"],
                    run["title"], run["res_model"], run["res_id"],
                    run["flow_id"], run["capability_id"],
                    meta=run.get("meta") or {})

    # -------------------------------------------------------------- #
    # _loop_save
    # -------------------------------------------------------------- #
    # ---- loop cursor persistence: one log row per loop ---------------- #
    @api.model
    def _loop_save(self, key, run, status="running"):
        Log = self.env["mmux.interaction.log"].sudo()
        existing = Log.search([("idempotency_key", "=", key)], limit=1)
        detail = json.dumps(run, ensure_ascii=False)
        vals = {"detail": detail, "exec_status": status,
                "capability_id_name": f"taskloop:{run.get('scenario', '')}",
                "scenario": run.get("scenario", "")}
        if existing:
            existing.write(vals)
        else:
            Log.create(dict(vals, idempotency_key=key,
                            user_id=self.env.user.id, input_type="combo"))

    # -------------------------------------------------------------- #
    # _loop_load
    # -------------------------------------------------------------- #
    @api.model
    def _loop_load(self, key):
        Log = self.env["mmux.interaction.log"].sudo()
        rec = Log.search([("idempotency_key", "=", key),
                          ("user_id", "=", self.env.user.id)], limit=1)
        if not rec or not rec.detail:
            return None
        try:
            return json.loads(rec.detail)
        except ValueError:
            return None
