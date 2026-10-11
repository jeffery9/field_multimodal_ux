# -*- coding: utf-8 -*-
"""HTTP API for the multimodal field assistant.

Every route runs under the *current user's* identity — no service-account
privilege escalation (PRD §6.2). CSRF protection stays on; the OWL client
sends the standard X-CSRF token.
"""
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class MmuxController(http.Controller):
    # -------------------------------------------------------------- #
    @http.route("/mmux/capabilities", type="json", auth="user")
    def capabilities(self, scenario=""):
        return request.env["mmux.service"].capabilities_for_user(scenario)

    @http.route("/mmux/transcribe", type="json", auth="user")
    def transcribe(self, audio_b64=None, filename="", text_hint=None, lang=None):
        if not audio_b64 and not text_hint:
            return {"error": "no audio payload", "fallback": "text"}
        return request.env["mmux.service"].transcribe(audio_b64, filename,
                                                      text_hint, lang=lang)

    @http.route("/mmux/vision", type="json", auth="user")
    def vision(self, image_b64=None, purpose="ocr", instruction=None):
        if not image_b64:
            return {"error": "no image payload"}
        return request.env["mmux.service"].analyze_image(image_b64, purpose, instruction)

    @http.route("/mmux/parse_intent", type="json", auth="user")
    def parse_intent(self, text=None, context=None, langs=None):
        """LLM-first semantic recognition. `langs` is accepted for
        backward compatibility but IGNORED — the LLM understands every
        language (including mixed input) and normalises to English.

        Returns either an intent dict (may be an *empty* intent when the
        text matches no capability — not an error) or a structured error
        {code: "llm_unavailable", retryable, ...} when the LLM path is
        unavailable. Both are passed through verbatim."""
        if not text or not str(text).strip():
            return {"error": "empty input"}
        return request.env["mmux.service"].parse_intent(
            text, context or {}, langs=langs)

    @http.route("/mmux/resolve", type="json", auth="user")
    def resolve(self, barcode=None, models_hint=None):
        if not barcode:
            return {"error": "empty barcode"}
        return {"candidates": request.env["mmux.service"].resolve_barcode(
            barcode, models_hint or ())}

    @http.route("/mmux/execute", type="json", auth="user")
    def execute(self, capability_id=None, params=None, record_id=None,
                idempotency_key=None, confirmed=False):
        """The single funnel for business execution. The idempotency key is
        mandatory for any non-L0 capability (PRD §6.4)."""
        if not capability_id:
            return {"status": "failed", "error": "missing capability_id"}
        if not idempotency_key:
            idempotency_key = None
        return request.env["mmux.service"].execute_capability(
            capability_id, params or {}, record_id=record_id,
            idem_key=idempotency_key, confirmed=bool(confirmed))

    # ---------------- plan orchestration (agent loop) ---------------- #
    @http.route("/mmux/plan/propose", type="json", auth="user")
    def plan_propose(self, text=None, context=None):
        """Propose a multi-step plan (library match or strictly-validated
        LLM proposal). Proposal never executes anything by itself."""
        if not text or not str(text).strip():
            return {"error": "empty input"}
        return request.env["mmux.service"].propose_plan(text, context or {})

    @http.route("/mmux/plan/run", type="json", auth="user")
    def plan_run(self, plan_id=None, llm_plan=None, inputs=None,
                 idempotency_key=None, confirmed_steps=None,
                 context_record=None):
        """Run a plan step-by-step. Each step still passes the execution
        guard (schema/ACL/state/risk/idempotency). The loop pauses on
        input_required / awaiting_confirmation; the client resumes with
        the same idempotency key plus the answered inputs or confirmed
        step ids."""
        return request.env["mmux.service"].plan_run(
            plan_id=plan_id, llm_plan=llm_plan, inputs=inputs,
            idempotency_key=idempotency_key,
            confirmed_steps=confirmed_steps,
            context_record=context_record)

    # ---------------- guided voice navigation ---------------- #
    @http.route("/mmux/guide/flows", type="json", auth="user")
    def guide_flows(self):
        return {"flows": request.env["mmux.service"].guide_flows()}

    @http.route("/mmux/guide/skill_level", type="json", auth="user")
    def guide_skill_level(self, level=None):
        """Read (no arg) or set (level=novice|expert) the user's guidance
        level. Affects step granularity/verbosity only, never authority."""
        svc = request.env["mmux.service"]
        if level:
            return svc.set_skill_level(level)
        return {"skill_level": svc._user_skill_level()}

    @http.route("/mmux/guide/start", type="json", auth="user")
    def guide_start(self, flow_id=None, params=None, mode="scripted"):
        """Start a guided run. `mode="agent"` hands the same flow to the
        agent loop (v3.0); the default stays scripted so existing callers
        and the agent's own fallback are unaffected."""
        if not flow_id:
            return {"error": "missing flow_id"}
        return request.env["mmux.service"].guide_start(flow_id, params,
                                                      mode=mode or "scripted")

    @http.route("/mmux/guide/current", type="json", auth="user")
    def guide_current(self, guide_key=None):
        if not guide_key:
            return {"error": "missing guide_key"}
        return request.env["mmux.service"].guide_current(guide_key)

    @http.route("/mmux/guide/advance", type="json", auth="user")
    def guide_advance(self, guide_key=None, step_id=None, input_data=None,
                      skip=False):
        """Complete/skip the current guided step. Capability-backed steps
        execute through the normal guard (zero extra authority)."""
        if not guide_key or not step_id:
            return {"error": "missing guide_key or step_id"}
        return request.env["mmux.service"].guide_advance(
            guide_key, step_id, input_data, skip=bool(skip))

    # ---------------- task loop (get -> do -> report -> next) --------- #
    @http.route("/mmux/task/sources", type="json", auth="user")
    def task_sources(self, scenario=""):
        """What counts as a task in this scenario (read-only contract)."""
        return {"sources": request.env["mmux.service"].task_sources(scenario)}

    @http.route("/mmux/task/next", type="json", auth="user")
    def task_next(self, scenario="", source_id=None, res_id=None):
        """Publish the next task for the current user (server-driven).

        `res_id` refreshes/recovers one exact record instead of taking the
        head of the priority queue. Nothing pending -> loop state
        `finished_all` (a normal end of the loop, not an error)."""
        return request.env["mmux.service"].task_next(
            scenario or "", source_id=source_id, res_id=res_id)

    @http.route("/mmux/task/current", type="json", auth="user")
    def task_current(self, loop_key=None):
        if not loop_key:
            return {"error": "missing loop_key"}
        return request.env["mmux.service"].task_current(loop_key)

    @http.route("/mmux/task/report", type="json", auth="user")
    def task_report(self, loop_key=None, text=None, intent=None, params=None,
                    confirmed=False):
        """Report the current task complete.

        Free `text` (any language, possibly mixed) goes through the
        LLM-first path and must resolve to the task's own completion
        capability; execution then passes the full ExecutionGuard, so an
        L2 completion parks on `awaiting_confirmation` instead of posting.
        On success the task is marked done and the NEXT task is returned."""
        if not loop_key:
            return {"error": "missing loop_key"}
        if not text and not intent:
            return {"error": "missing completion report"}
        return request.env["mmux.service"].task_report(
            loop_key, text=text, intent=intent, params=params or {},
            confirmed=bool(confirmed))

    @http.route("/mmux/task/guide", type="json", auth="user")
    def task_guide(self, loop_key=None, mode=None):
        """Start/resume guided navigation for the current task.

        `mode="agent"` drives the same recipe through the agent loop;
        unset means "agent when enabled, scripted otherwise"."""
        if not loop_key:
            return {"error": "missing loop_key"}
        return request.env["mmux.service"].task_guide(loop_key, mode=mode)

    @http.route("/mmux/task/help", type="json", auth="user")
    def task_help(self, loop_key=None, text=None, intent=None):
        """Ask for help mid-task. Returns either `action: "execute"` (the
        question resolved to a whitelisted capability) or `action:
        "guidance"` — help is never a silent business write."""
        if not loop_key:
            return {"error": "missing loop_key"}
        if not text and not intent:
            return {"error": "missing question"}
        return request.env["mmux.service"].task_help(
            loop_key, text=text, intent=intent)

    # ---------------- agent loop (pure agent, v3.0) ------------------- #
    @http.route("/mmux/agent/start", type="json", auth="user")
    def agent_start(self, loop_key=None, loop=None, goal=None, signal=None):
        """Start adopting an agent loop.

        With `loop_key` the agent adopts an existing task loop (task, flow
        and scenario become its mission) — this is how the task-loop UI
        upgrades to the agent without the operator doing anything
        different. With `loop` the caller describes the work explicitly.

        `signal` (v3.1) is WHERE the operator is working — the menu/action
        they came through, the record on screen, optionally a position.
        The server turns it into candidate scenarios; the client never
        names the scenario itself, because that would let the browser pick
        the capability whitelist its own answers are checked against.

        Omitting `signal` entirely means "older client, not applicable"
        and keeps the v3.0 behaviour byte for byte. Sending `signal={}`
        means "I tried and had nothing to report", which is a different
        statement and is answered with a question.

        Returns the loop key, the first turn, the recipe position, the
        budgets and the trace. Returns `{code: "agent_disabled"}` when the
        feature flag is off, so a client can fall back to the scripted
        guide rather than treating it as a failure."""
        return request.env["mmux.service"].agent_start(
            loop_key=loop_key, loop=loop, goal=goal or "", signal=signal)

    @http.route("/mmux/agent/turn", type="json", auth="user")
    def agent_turn(self, loop_key=None, answer=None, confirmed=False):
        """Advance the agent loop by ONE step.

        One step per request on purpose: a single server-side loop would
        hold one transaction across several LLM round trips and several
        writes, and would leave no moment at which to surface
        `awaiting_confirmation` (the human gate) to the operator.

        `answer` supplies a value for a previous `ask_user`. `confirmed`
        only releases the capability the guard ITSELF parked — it is never
        forwarded into the reasoning step, so confirming cannot change
        what the agent proposes."""
        if not loop_key:
            return {"error": "missing loop_key"}
        return request.env["mmux.service"].agent_turn(
            loop_key, answer=answer, confirmed=bool(confirmed))
