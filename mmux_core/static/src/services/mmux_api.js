/**
 * Copyright (C) 2026 jeffery9@gmail.com & geninit
 * License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
 */
/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { jsonrpc } from "@web/core/utils/rpc";

/**
 * Thin RPC client for the /mmux/* JSON routes. All calls carry the current
 * user session; the CSRF token is handled by Odoo's rpc layer.
 */
export class MmuxApi {
    constructor() {}

    capabilities(scenario = "") {
        return jsonrpc("/mmux/capabilities", { scenario });
    }
    transcribe(audioB64, filename = "", textHint = null, lang = null) {
        return jsonrpc("/mmux/transcribe", {
            audio_b64: audioB64, filename, text_hint: textHint, lang,
        });
    }
    vision(imageB64, purpose = "ocr", instruction = null) {
        return jsonrpc("/mmux/vision", { image_b64: imageB64, purpose, instruction });
    }
    /** langs: optional forced subset; default = auto-detect per call, so
     * multilingual / mixed-language utterances are handled natively. */
    // LLM-first recognition. `langs` is kept for backward compatibility
    // but ignored server-side: the LLM understands all languages and
    // normalises to English. Response is either an intent dict (possibly
    // empty when nothing matched) or {code: "llm_unavailable", retryable}.
    parseIntent(text, context = {}, langs = null) {
        return jsonrpc("/mmux/parse_intent", { text, context, langs });
    }
    resolve(barcode, modelsHint = null) {
        return jsonrpc("/mmux/resolve", { barcode, models_hint: modelsHint });
    }
    execute(capabilityId, params, recordId = null, idempotencyKey = null, confirmed = false) {
        return jsonrpc("/mmux/execute", {
            capability_id: capabilityId,
            params,
            record_id: recordId,
            idempotency_key: idempotencyKey,
            confirmed,
        });
    }
    /** One idempotency key per user intent; reused across retries (PRD 6.4). */
    newIdempotencyKey() {
        const uuid = (crypto.randomUUID && crypto.randomUUID()) ||
            "xxxxxxxxyxxx".replace(/[xy]/g, (c) => {
                const r = (Math.random() * 16) | 0;
                return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
            });
        return `mmux:${uuid}`;
    }
    /** Plan orchestration (agent loop): propose never executes. */
    planPropose(text, context = {}) {
        return jsonrpc("/mmux/plan/propose", { text, context });
    }
    planRun({ plan_id = null, llm_plan = null, inputs = {}, idempotency_key = null,
              confirmed_steps = [], context_record = null }) {
        return jsonrpc("/mmux/plan/run", {
            plan_id, llm_plan, inputs,
            idempotency_key, confirmed_steps, context_record,
        });
    }
    /** Guided voice navigation. */
    guideFlows() {
        return jsonrpc("/mmux/guide/flows", {});
    }
    guideStart(flowId, params = null) {
        return jsonrpc("/mmux/guide/start", { flow_id: flowId, params });
    }
    guideCurrent(guideKey) {
        return jsonrpc("/mmux/guide/current", { guide_key: guideKey });
    }
    guideAdvance(guideKey, stepId, inputData = null, skip = false) {
        return jsonrpc("/mmux/guide/advance", {
            guide_key: guideKey, step_id: stepId,
            input_data: inputData, skip,
        });
    }
    /** Skill level: get (no arg) or set (novice | expert). */
    guideSkillLevel(level = null) {
        return jsonrpc("/mmux/guide/skill_level", { level });
    }

    /* ---------------- task loop: get -> do -> report -> next -------- */
    /** What counts as a task in this scenario (read-only contract). */
    taskSources(scenario = "") {
        return jsonrpc("/mmux/task/sources", { scenario });
    }
    /** Publish the next task (server-driven priority queue).
     * `resId` refreshes/recovers one exact record instead of the head. */
    taskNext(scenario = "", sourceId = null, resId = null) {
        return jsonrpc("/mmux/task/next", {
            scenario, source_id: sourceId, res_id: resId,
        });
    }
    taskCurrent(loopKey) {
        return jsonrpc("/mmux/task/current", { loop_key: loopKey });
    }
    /** Report the task complete. `text` may be any language / mixed;
     * the backend normalises it and must match the task's completion
     * capability. Execution still passes the full guard (L2 parks). */
    taskReport(loopKey, { text = null, intent = null, params = {}, confirmed = false } = {}) {
        return jsonrpc("/mmux/task/report", {
            loop_key: loopKey, text, intent, params, confirmed,
        });
    }
    /** Start/resume the guided flow bound to the current task.
     * `mode`: "agent" | "scripted" | null (= agent when enabled). */
    taskGuide(loopKey, mode = null) {
        return jsonrpc("/mmux/task/guide", { loop_key: loopKey, mode });
    }
    /** Ask for help mid-task: {action: "execute"|"guidance", ...}. */
    taskHelp(loopKey, text = null, intent = null) {
        return jsonrpc("/mmux/task/help", {
            loop_key: loopKey, text, intent,
        });
    }

    /* ------------- pure agent loop (v3.0) --------------------------- */
    /**
     * Start (or adopt) an agent loop and get its FIRST turn back.
     *
     * `loopKey` adopts an existing v2.x task loop (its task/flow/scenario
     * become the agent's mission) — that is how this UI upgrades to the
     * agent without the operator doing anything differently. `loop` is
     * for callers with no task loop of their own.
     *
     * `signal` (v3.1) is the position signal from `mmux_context`, and it
     * is passed through UNCHANGED — including when it is null/undefined.
     * That distinction is load-bearing: `signal: undefined` means "this
     * client does not report position" and keeps the v3.0 behaviour,
     * whereas `signal: {}` means "I looked and found nothing" and is
     * answered with a question to the operator. Coercing one into the
     * other with a `|| {}` here would make every existing client start
     * interrogating its user.
     *
     * The server mints and returns `loop_key`, so the client only ever
     * holds a key — never the agent's memory. A page reload resumes by
     * passing that key back to agentTurn.
     */
    agentStart({ loopKey = null, loop = null, goal = "", signal } = {}) {
        const params = { loop_key: loopKey, loop, goal };
        if (signal !== undefined && signal !== null) {
            params.signal = signal;
        }
        return jsonrpc("/mmux/agent/start", params);
    }
    /**
     * Advance the agent loop by exactly ONE step.
     *
     * One step per request on purpose: a server-side loop would hold one
     * transaction across several LLM round-trips and several writes, and
     * would leave no moment to surface `awaiting_confirmation` — the
     * human gate — to the operator.
     *
     * `answer` supplies a value for a previous `ask_user`. `confirmed`
     * ONLY releases the capability the guard itself parked; it is never
     * forwarded into the reasoning step, so confirming can never change
     * what the agent proposes next.
     */
    agentTurn(loopKey, { answer = null, confirmed = false } = {}) {
        return jsonrpc("/mmux/agent/turn", {
            loop_key: loopKey, answer, confirmed,
        });
    }
}

export const mmuxApi = new MmuxApi();
export const STATUS_LABELS = {
    recognized: _t("Recognized"),
    validated: _t("Validated"),
    awaiting_confirmation: _t("Awaiting confirmation"),
    submitted: _t("Submitted"),
    success: _t("Success"),
    failed: _t("Failed"),
    unknown: _t("Result unknown — verify record state"),
};

/**
 * Agent loop vocabulary.
 *
 * These mirror `mmux_core/services/mmux_core/agentloop.py` exactly. They
 * are display names ONLY: the frontend never validates against them, and
 * an action or state it does not recognize is rendered by its raw value
 * rather than hidden — a new server-side action must never become an
 * invisible gap in the trace.
 */
export const AGENT_ACTION_LABELS = {
    call_capability: _t("Call capability"),
    propose_step: _t("Propose step"),
    ask_user: _t("Ask user"),
    answer: _t("Answer"),
    finish: _t("Finish"),
    delegate_pick: _t("Hand over task pick"),
    perceive_image: _t("Look at the image"),
};

export const AGENT_STATE_LABELS = {
    running: _t("Running"),
    awaiting_confirmation: _t("Awaiting confirmation"),
    awaiting_user: _t("Awaiting your answer"),
    finished: _t("Finished"),
    finished_all: _t("All done"),
    failed: _t("Stopped"),
    budget_exhausted: _t("Budget exhausted"),
};

export const AGENT_SOURCE_LABELS = {
    llm: _t("Model decision"),
    recipe_fallback: _t("Recipe fallback"),
    no_provider: _t("No model — scripted"),
};

/**
 * Scenario provenance vocabulary (v3.1).
 *
 * Mirrors `mmux_core/services/mmux_core/scenario.py`. Display names ONLY:
 * the frontend never decides a scenario and never validates one against
 * these — the server derives the candidate set and validates the choice.
 */
export const SCENARIO_SOURCE_LABELS = {
    signal: _t("From your position"),
    ambiguous: _t("Position is ambiguous"),
    none: _t("Position unknown"),
    user: _t("You chose this"),
};
