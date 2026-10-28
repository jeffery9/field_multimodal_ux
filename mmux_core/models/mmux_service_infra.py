# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Infrastructure: provider wiring, the execution guard and idempotency.
These methods call no other business method, so they sit at the bottom
of the dependency graph and every other file may rely on them.
"""
import json
import logging

_logger = logging.getLogger(__name__)

from odoo import api, models

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
    MAX_AGENT_TURNS, MAX_AGENT_CAPABILITY_CALLS, MAX_REPEATED_STEP,
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
    # _provider_config
    # -------------------------------------------------------------- #
    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    @api.model
    def _provider_config(self):
        """Read provider configuration from ir.config_parameter.

        These are the same keys the settings form declares via
        ``config_parameter=``, so the form and this reader can never
        disagree: there is one copy of the value, in one place.

        The read is deliberately direct. Routing it through the settings
        model would add a second code path that could disagree with this
        one, for no gain — the form already writes to exactly these keys.
        """
        Param = self.env["ir.config_parameter"].sudo()
        keys = ("mmux.asr.provider", "mmux.llm.provider", "mmux.vision.provider",
                "mmux.ocr.provider", "mmux.asr.base_url", "mmux.asr.api_key",
                "mmux.asr.model", "mmux.asr.language", "mmux.asr.timeout",
                "mmux.llm.base_url", "mmux.llm.api_key", "mmux.llm.model",
                "mmux.llm.timeout", "mmux.vision.model",
                "mmux.asr.mock_transcript", "mmux.llm.mock_response",
                "mmux.llm.mock_error")
        return {k: Param.get_param(k) for k in keys}

    # -------------------------------------------------------------- #
    # _sync_capability_toggles
    # -------------------------------------------------------------- #
    @api.model
    def _sync_capability_toggles(self):
        """Apply DB capability switches (mmux.capability.toggle) to the
        in-memory registry.

        This is the ONLY runtime write point for `capability.enabled`.
        The pure-core guard (`guard.py`) and plan orchestrator
        (`plan.py`) read the registry directly and cannot touch the
        database (the pure core may not import odoo), so the DB rows are
        folded into the shared in-memory singleton here, at the two hot
        read paths that already exist: `_caps_payload` and
        `_agent_allowed_capabilities`. Everything downstream (guard,
        plans, agent whitelist, capability view) then sees the change
        with zero further code.

        Cost control: a signature (sorted id+enabled pairs) is compared
        against the one cached on the registry; identical means nothing
        changed since the last sync in this worker and the search result
        is discarded after one comparison. The table is tiny (one row
        per admin-toggled capability, normally zero), so the search
        itself is a primary-key-scan on a near-empty table.

        Multi-worker consistency: each worker process holds its own
        registry copy, so a toggle lands on each worker at its next
        hot-path read. That window is availability-only — disabling a
        capability late cannot weaken safety (the L2 confirmation gate
        is independent of `enabled`), and enabling late only delays
        availability. Never a security degradation.
        """
        # sudo: mmux.capability.toggle is manager-only by ACL; the
        # service layer must read the switches to serve every worker's
        # whitelist, which is elevation by design (rule 6).
        rows = self.env["mmux.capability.toggle"].sudo().search([])
        sig = tuple(sorted((r.capability_id, r.enabled) for r in rows))
        reg = self.env.registry
        if getattr(reg, "_mmux_toggle_sig", None) == sig:
            return
        for r in rows:
            cap = CAPABILITY_REGISTRY.get(r.capability_id)
            if cap is not None:
                cap.enabled = bool(r.enabled)
        reg._mmux_toggle_sig = sig
        # defence in depth: the fingerprint check in `_caps_payload`
        # already invalidates on its own (the fingerprint is recomputed
        # from the live registry on every call), so dropping the cache
        # here is redundant — deliberately. Two independent guards mean
        # removing either one alone never leaves a stale payload.
        reg._mmux_caps_payload = None

    # -------------------------------------------------------------- #
    # _caps_payload
    # -------------------------------------------------------------- #
    @api.model
    def _caps_payload(self) -> list:
        """The enabled capabilities, as public dicts, for prompt building.

        Every LLM prompt that offers the model a capability list embeds
        this — the intent router and the plan proposer both do — and it
        was rebuilt from scratch each time.  Each rebuild walks every
        capability and serialises its whole parameter schema, so on a
        hot path (one NL utterance = one prompt) that is pure repeated
        work: the payload is derived from code (plus DB toggles applied
        by `_sync_capability_toggles` below) and the answer cannot
        change between two calls in the same request.

        Cached on the registry, like `_parser` and `_orchestrator`: the
        payload is derived from code, carries no user data and no
        per-request state, so one copy per worker is correct.

        Keyed by a **fingerprint** rather than a length counter.
        Capability switches change the payload without changing how
        many capabilities exist.  Comparing the id set keeps the cache
        honest in that case for the cost of a sorted tuple of strings —
        far cheaper than the serialisation it avoids.  The signature
        cache in `_sync_capability_toggles` drops this cache whenever a
        switch changes, so the two caches cannot disagree.
        """
        self._sync_capability_toggles()
        fingerprint = tuple(sorted(
            c.capability_id for c in CAPABILITY_REGISTRY.all() if c.enabled))
        cached = getattr(self.env.registry, "_mmux_caps_payload", None)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        payload = [c.to_public_dict()
                   for c in CAPABILITY_REGISTRY.all() if c.enabled]
        self.env.registry._mmux_caps_payload = (fingerprint, payload)
        return payload

    # -------------------------------------------------------------- #
    # _parser
    # -------------------------------------------------------------- #
    @api.model
    def _parser(self) -> IntentParser:
        """The LLM-output validator. There is no rule parser any more;
        this only builds the whitelist/schema checker."""
        parser = getattr(self.env.registry, "_mmux_parser", None)
        if parser is None:
            parser = IntentParser(CAPABILITY_REGISTRY)
            self.env.registry._mmux_parser = parser
        return parser

    # -------------------------------------------------------------- #
    # _guard
    # -------------------------------------------------------------- #
    @api.model
    def _guard(self) -> ExecutionGuard:
        return ExecutionGuard(CAPABILITY_REGISTRY, self)

    # -------------------------------------------------------------- #
    # _orchestrator
    # -------------------------------------------------------------- #
    # ================================================================== #
    # Plan orchestration (agent loop, PRD-constrained)
    # ================================================================== #
    @api.model
    def _orchestrator(self) -> PlanOrchestrator:
        orch = getattr(self.env.registry, "_mmux_orchestrator", None)
        if orch is None:
            orch = PlanOrchestrator(CAPABILITY_REGISTRY)
            self.env.registry._mmux_orchestrator = orch
        return orch

    # -------------------------------------------------------------- #
    # _extract_llm_data
    # -------------------------------------------------------------- #
    @api.model
    def _extract_llm_data(self, raw):
        """Pull the JSON object out of a provider answer.

        Tolerant about the envelope (a `{"data": {...}}` wrapper, a
        `{"raw": "<json text>"}` wrapper, or a bare JSON string), strict
        about the content — the caller validates what comes back. Returns
        None when nothing usable was produced, which is a degradation
        rather than an error.

        The `raw` fallback matters: a provider that received text from the
        model but could not parse it will report `data: None` *and* keep
        the text in `raw`. Walking straight to `data` would throw away an
        answer we can actually parse, and turn a perfectly good
        disambiguation into a question to the operator.
        """
        if raw is None:
            return None
        if isinstance(raw, dict):
            for key in ("data", "content", "raw"):
                if key in raw:
                    found = self._extract_llm_data(raw.get(key))
                    if found is not None:
                        return found
            # no recognised envelope key: the dict IS the payload
            return raw or None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except ValueError:
                return None
        return None

    # -------------------------------------------------------------- #
    # _agent_llm_call
    # -------------------------------------------------------------- #
    @api.model
    def _agent_llm_call(self):
        """Return the provider callable, or None when no LLM is usable.

        Returning None (rather than raising) is what lets the pure loop
        fall back to the recipe: `run_loop` sees `llm_call is None` and
        narrates deterministically instead of failing.
        """
        config = self._provider_config()
        provider = config.get("mmux.llm.provider")
        if not provider:
            return None, config
        if provider != "mock" and not config.get("mmux.llm.api_key"):
            return None, config

        def call(payload, _config=None):
            return PROVIDER_REGISTRY.call("llm", provider, payload, config)

        return call, config

    # -------------------------------------------------------------- #
    # _agent_enabled
    # -------------------------------------------------------------- #
    # ================================================================== #
    # Agent loop (v3.0 "pure agent"): the model decides, the guard rules
    # ================================================================== #
    # Architecture note, because this is the part most likely to be
    # "simplified" by someone who has not read the PRD:
    #
    #   "pure agent" means the DECISION moved to the model — what happens
    #   next, in what order, and why. It does NOT mean authority moved.
    #   Every write still travels `execute_capability -> ExecutionGuard`,
    #   every L2 write still parks on human confirmation, and the agent
    #   has no action that bypasses either. `execution_request()` in the
    #   pure core hardwires `confirmed=False`; there is deliberately no
    #   parameter anywhere in this section that could override it.
    #
    # The loop is driven ONE STEP PER REQUEST by the client. That is not
    # an accident of the HTTP shape: a single server-side loop that ran to
    # completion would hold one transaction across several LLM round trips
    # and several writes, and would make `awaiting_confirmation` impossible
    # to surface (there would be no moment at which to ask the human).
    # ------------------------------------------------------------------ #
    @api.model
    def _agent_enabled(self):
        """Feature flag. Off, the agent loop is not offered at all and the
        v2.x guided flow is the only path (progressive enhancement).

        Read straight from the parameter, which is the same key the
        settings form writes via ``config_parameter="mmux.agent.enabled"``.
        A direct read keeps this usable in environments without the
        settings model (the pure harnesses) and keeps the flag's cost to
        one dictionary lookup on a hot path.
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "mmux.agent.enabled", "1")
        return str(raw).lower() not in ("0", "false", "no", "")

    # -------------------------------------------------------------- #
    # _agent_budgets
    # -------------------------------------------------------------- #
    @api.model
    def _agent_budgets(self):
        """The agent's hard budgets, per deployment (v19.0.6).

        Keys are the same ones the settings form writes via
        ``config_parameter=``, so the form and this reader can never
        disagree. Values invalid or absent fall back to the pure-core
        module defaults — an unreadable budget must never mean
        "unlimited", and the settings form's own constraint makes that
        path unreachable from the UI (this is defence in depth).

        No caching here, deliberately: ``get_param`` is already
        ``@ormcache('key', cache='stable')`` and every write clears it,
        so a mid-session budget change takes effect on the NEXT turn.
        """
        # sudo: ir.config_parameter reads are elevated by design
        # (check_standards rule 6 whitelist) — the settings form writes
        # through the same elevated path.
        Param = self.env["ir.config_parameter"].sudo()

        def _int(key, default):
            try:
                value = int(Param.get_param(key) or default)
            except (TypeError, ValueError):
                return default
            return value if value >= 1 else default

        return {
            "max_turns": _int("mmux.agent.max_turns", MAX_AGENT_TURNS),
            "max_capability_calls": _int(
                "mmux.agent.max_capability_calls",
                MAX_AGENT_CAPABILITY_CALLS),
            "max_repeated_step": _int(
                "mmux.agent.max_repeated_step", MAX_REPEATED_STEP),
        }

    # -------------------------------------------------------------- #
    # _attach_photos
    # -------------------------------------------------------------- #
    @api.model
    def _attach_photos(self, record, photos, subject=None):
        """Store field photos as real attachments on `record`.

        Returns the list of attachment ids, ready for
        ``record.message_post(attachment_ids=...)``.

        Two things make this deliberately boring, and both matter:

        * **It is called from inside a capability executor**, so it runs
          on the same transaction as the write the worker actually asked
          for, and under the same `ExecutionGuard` access check. A
          separate upload route would create a second write surface that
          skips ACL, record rules, idempotency and the audit trail.
        * **Create-then-post, same transaction.** If `message_post` raises
          the whole thing rolls back, so there are no orphaned
          attachments pointing at a message that never existed.

        Photos arrive as ``[{"image_b64": ..., "name": ..., "mimetype":
        ...}]``. Entries without image data are skipped rather than
        raising: a camera that failed should not fail the work report it
        was attached to.
        """
        Attachment = self.env["ir.attachment"]
        attachment_ids = []
        for photo in photos or []:
            if not isinstance(photo, dict):
                continue
            data = photo.get("image_b64") or photo.get("datas") or ""
            # Callers pass a bare base64 body; tolerate a data URL too.
            data = str(data).split(",")[-1]
            if not data:
                continue
            attachment = Attachment.create({
                "name": photo.get("name") or "field_evidence.jpg",
                "datas": data,
                "res_model": record._name,
                "res_id": record.id,
                "mimetype": photo.get("mimetype") or "image/jpeg",
            })
            attachment_ids.append(attachment.id)
        return attachment_ids

    # -------------------------------------------------------------- #
    # _agent_allowed_capabilities
    # -------------------------------------------------------------- #
    @api.model
    def _agent_allowed_capabilities(self, scenario=""):
        """The authoritative whitelist for this loop.

        Enabled capabilities only — and *scenario-scoped* when we know the
        scenario, so a warehouse agent is not offered manufacturing
        capabilities. This list is both offered to the model and used to
        validate its answer, from one source, so the two can never drift.
        """
        # DB switches first: the whitelist must reflect the admin's
        # toggles before it is offered (and enforced).
        self._sync_capability_toggles()
        caps = CAPABILITY_REGISTRY.for_scenario(scenario) if scenario \
            else CAPABILITY_REGISTRY.all()
        return [c.capability_id for c in caps if c.enabled]

    # -------------------------------------------------------------- #
    # has_user_group
    # -------------------------------------------------------------- #
    # ------------------------------------------------------------------ #
    # BackendOps implementation for ExecutionGuard
    # ------------------------------------------------------------------ #
    @api.model
    def has_user_group(self, group_xmlid):
        return self.user_has_groups(group_xmlid)

    # -------------------------------------------------------------- #
    # is_duplicate
    # -------------------------------------------------------------- #
    @api.model
    def is_duplicate(self, key):
        if not key:
            return False
        Log = self.env["mmux.interaction.log"]
        return bool(Log.sudo().search_count([
            ("idempotency_key", "=", key),
            ("exec_status", "in", [ExecStatus.SUBMITTED.value, ExecStatus.SUCCESS.value, ExecStatus.UNKNOWN.value]),
        ]))

    # -------------------------------------------------------------- #
    # mark_executed
    # -------------------------------------------------------------- #
    @api.model
    def mark_executed(self, key, status):
        if not key:
            return
        Log = self.env["mmux.interaction.log"].sudo()
        existing = Log.search([("idempotency_key", "=", key)], limit=1)
        vals = {"exec_status": status}
        if existing:
            existing.write(vals)
        else:
            Log.create(dict(vals, idempotency_key=key, user_id=self.env.user.id))

    # -------------------------------------------------------------- #
    # llm_unavailable
    # -------------------------------------------------------------- #
    @api.model
    def llm_unavailable(self, message, retryable=False, subtag=None):
        """Structured error returned when the LLM semantic path cannot run
        (not configured / timeout / network error / invalid JSON). The
        frontend branches on `code` and retries only when `retryable`."""
        detail = {"subtag": subtag} if subtag else {}
        return {"error": str(message), "code": LLM_UNAVAILABLE,
                "retryable": bool(retryable), "detail": detail}
