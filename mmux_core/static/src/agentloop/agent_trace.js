/** @odoo-module **/

import { Component, onWillUnmount, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { mmuxApi, AGENT_ACTION_LABELS, AGENT_STATE_LABELS, AGENT_SOURCE_LABELS,
         SCENARIO_SOURCE_LABELS } from "../services/mmux_api";
import { tts } from "../services/tts";
import { speakGuarded } from "../services/speech";
import { LOOP_STATES, voiceLoop } from "../services/voice_loop";
import { normalizePhrase } from "../services/voice_logic";

/**
 * The agent trace — a view onto ONE running agent loop.
 *
 * Architecture note (v3.0 "pure agent"): this component is a *renderer
 * and a pump*, not a controller. It never decides what happens next.
 *
 *   - the **server** owns the decision: it asks the model (or falls back
 *     to the flow's recipe) and validates the answer against the closed
 *     action set;
 *   - this component's only job is to ask for **one more turn** and draw
 *     what came back;
 *   - every capability the agent proposes travels the SAME funnel
 *     (`execute_capability` -> ExecutionGuard) as a manual action.
 *     Nothing here can widen that: there is no execute path in this file
 *     and no place a `confirmed` flag could be invented.
 *
 * `confirmed` is the single deliberate exception, and it is narrow: the
 * button that sends it appears *only* for `state ===
 * "awaiting_confirmation"`, which the server sets when the GUARD itself
 * parked a write. The server then re-submits exactly the request it
 * parked, from its own persisted state — so this flag releases a
 * decision the human already owns; it can never authorize a new one.
 *
 * "Why did it do that?" is not a debug view bolted on afterwards. It is
 * the same turn the loop acted on: the reason, the source (model vs
 * recipe fallback), the budget counters and the rejected actions are all
 * first-class, because an agent that decides for a human has to be able
 * to show its reasoning to that human. A degraded turn is labelled as
 * degraded — it must never masquerade as a considered decision.
 *
 * v3.1 adds the SCENARIO CONTEXT bar. It reports where the server thinks
 * the operator is working, on what evidence, and whether a position was
 * seen but ignored. Two properties matter:
 *
 *   - this component SENDS the position signal but never computes one, so
 *     the derivation stays auditable server-side (see `context_service`);
 *   - when the operator's stated scenario and the observed position
 *     disagree, the disagreement is DISPLAYED rather than resolved.
 *     Silently "helping" by switching to the observed scenario would
 *     hand the client's guess authority over the operator's instruction.
 */
export class MmuxAgentTrace extends Component {
    static template = "mmux_core.AgentTrace";
    static props = {
        /** An existing v2.x task loop to adopt (task/flow/scenario). */
        loopKey: { type: [String, Boolean], optional: true },
        /** Explicit work description when there is no task loop. */
        loop: { type: [Object, Boolean], optional: true },
        /** Show the always-available "start" button when idle. */
        autostart: { type: Boolean, optional: true },
        /** Collapse the trace log behind a toggle. */
        collapsible: { type: Boolean, optional: true },
    };
    static defaultProps = { loopKey: null, loop: null, autostart: false, collapsible: false };

    setup() {
        this.api = mmuxApi;
        this.notification = useService("notification");
        this.context = useService("mmux_context");
        this.state = useState({
            loopKey: this.props.loopKey || null,
            view: null,              // the latest server view (one turn)
            trace: [],               // every turn rendered so far (append-only)
            askAnswer: "",
            busy: false,
            autoPumped: false,       // a mid-loop continuation is already scheduled
            showTrace: !this.props.collapsible,
            error: "",
            errorCode: "",
            // hands-free loop
            handsFree: false,
            loopState: "idle",
            voiceNotice: "",
            lastSpoken: "",
        });
        onWillUnmount(() => {
            this._alive = false;
            this._stopHandsFree();
            tts.stop();
        });
        this._alive = true;
        if (this.props.autostart && this.props.loopKey) {
            // adopting an existing task loop: start straight away. The
            // operator asked for guidance, not for a second button.
            this.start();
        }
    }

    // ---------------------------------------------------------------- //
    // derived flags — every one of them reads the SERVER's state
    // ---------------------------------------------------------------- //
    get view() {
        return this.state.view;
    }

    get turn() {
        return (this.state.view && this.state.view.turn) || null;
    }

    get action() {
        return (this.turn && this.turn.action) || null;
    }

    get isAwaitingConfirmation() {
        return !!(this.state.view && this.state.view.awaiting_confirmation);
    }

    get isAwaitingUser() {
        return !!(this.state.view && this.state.view.awaiting_user);
    }

    get isFinished() {
        const st = this.state.view && this.state.view.state;
        return st === "finished" || st === "finished_all";
    }

    get isStopped() {
        const st = this.state.view && this.state.view.state;
        return st === "failed" || st === "budget_exhausted";
    }

    /** The loop no longer moves on its own: a human is now the driver. */
    get isParked() {
        return this.isAwaitingConfirmation || this.isAwaitingUser || this.isFinished || this.isStopped;
    }

    get taskCompleted() {
        return !!(this.state.view && this.state.view.task_completed);
    }

    get agentDisabled() {
        return !!(this.state.error && this.state.errorCode === "agent_disabled");
    }

    get budget() {
        return (this.state.view && this.state.view.budget) || null;
    }

    get progress() {
        return (this.state.view && this.state.view.progress) || null;
    }

    get sourceLabel() {
        const src = (this.state.view && this.state.view.source) || "";
        return AGENT_SOURCE_LABELS[src] || src;
    }

    get stateLabel() {
        const st = (this.state.view && this.state.view.state) || "";
        return AGENT_STATE_LABELS[st] || st;
    }

    get isDegraded() {
        const src = (this.state.view && this.state.view.source) || "";
        return src === "recipe_fallback" || src === "no_provider";
    }

    /** A rejected model answer is evidence about the agent, not noise. */
    get rejectedReason() {
        return (this.turn && this.turn.rejected_reason) || null;
    }

    get execConfirmation() {
        const exec = (this.turn && this.turn.exec) || null;
        return (exec && exec.confirmation) || null;
    }

    get pendingCapability() {
        const act = this.action;
        return (act && act.payload && act.payload.capability_id) || "";
    }

    get pendingField() {
        return (this.state.view && this.state.view.pending_field) || "";
    }

    get pendingQuestion() {
        return (this.state.view && this.state.view.pending_question) || "";
    }

    get pendingOptions() {
        return (this.state.view && this.state.view.pending_options) || [];
    }

    /** The scenario-context block, or null when the feature does not apply. */
    get scenarioContext() {
        return (this.state.view && this.state.view.scenario_context) || null;
    }

    /** Human-readable provenance for a derived scenario. */
    scenarioSourceLabel(source) {
        const ctx = this.scenarioContext;
        const code = source || (ctx && ctx.source) || "";
        return SCENARIO_SOURCE_LABELS[code] || code;
    }

    /** Display name for a scenario code, preferring the server's label. */
    candidateLabel(code) {
        if (!code) {
            return "";
        }
        const ctx = this.scenarioContext;
        const found = ((ctx && ctx.candidates) || [])
            .find((c) => c.value === code);
        return (found && found.label) || code;
    }

    get speakText() {
        return (this.state.view && this.state.view.speak) || "";
    }

    /** Trace rows, oldest first, newest last — the reasoning as it unfolded. */
    get traceRows() {
        return this.state.trace;
    }

    get showsTraceToggle() {
        return !!this.props.collapsible && this.state.trace.length > 1;
    }

    actionLabel(entry) {
        const act = entry && entry.action;
        if (!act || !act.action) {
            return _t("(no action)");
        }
        return AGENT_ACTION_LABELS[act.action] || act.action;
    }

    /** Localize a source code coming from a trace row. */
    sourceLabelFor(source) {
        return AGENT_SOURCE_LABELS[source] || source;
    }

    /** One-line summary of a trace row, in the operator's language. */
    rowSummary(entry) {
        const act = entry && entry.action;
        const payload = (act && act.payload) || {};
        if (!act || !act.action) {
            return entry && entry.rejected_reason ? entry.rejected_reason : "";
        }
        switch (act.action) {
            case "call_capability":
                return payload.capability_id || "";
            case "propose_step":
                return payload.title || payload.step_id || "";
            case "ask_user":
                return payload.question || payload.field || "";
            case "answer":
                return payload.text || "";
            case "finish":
                return payload.summary || "";
            default:
                return "";
        }
    }

    /** The model's own justification, when it gave one. */
    rowWhy(entry) {
        const payload = (entry && entry.action && entry.action.payload) || {};
        return payload.why || "";
    }

    execLabel(entry) {
        const exec = entry && entry.exec;
        const status = exec && exec.status;
        if (!status) {
            return "";
        }
        return status;
    }

    // ---------------------------------------------------------------- //
    // the pump — exactly one turn per user gesture or per mid-loop step
    // ---------------------------------------------------------------- //
    async start() {
        if (this.state.busy) {
            return;
        }
        this.state.busy = true;
        this.state.error = "";
        this.state.errorCode = "";
        try {
            const res = await this.api.agentStart({
                loopKey: this.props.loopKey || this.state.loopKey || null,
                loop: this.props.loop || null,
                // The POSITION SIGNAL. `getSignal` returns null when the
                // client has nothing to report, and `agentStart` omits the
                // key entirely in that case — which the server reads as
                // "this client does not report position" and answers
                // exactly as v3.0 did. Sending `{}` instead would mean
                // something different ("I looked, there was nothing") and
                // would start asking the operator to pick a scenario.
                signal: this._currentSignal(),
            });
            this._apply(res);
        } catch (err) {
            this.state.error = _t("无法启动智能体循环");
            this.state.errorCode = "rpc_failed";
        } finally {
            this.state.busy = false;
        }
    }

    /**
     * Read the position signal from the context service.
     *
     * Deliberately total: a context service that is missing, throwing, or
     * returning nonsense yields `null` (= "not applicable") rather than an
     * empty object. Degrading to "not applicable" keeps the previous
     * behaviour; degrading to "{}" would invent a question.
     *
     * `geo_collect` is taken from the SERVER's previous answer, so the
     * browser is only ever asked for a position because an admin enabled
     * it — never because the client decided to.
     */
    _currentSignal() {
        if (!this.context || typeof this.context.getSignal !== "function") {
            return null;
        }
        try {
            const prev = this.state.view;
            return this.context.getSignal({
                geo_collect: !!(prev && prev.geo_collect),
                loop: this.props.loop || null,
            });
        } catch (err) {
            return null;
        }
    }

    /** Advance one step. `opts` is only ever {answer} or {confirmed}. */
    async advance(opts = {}) {
        if (this.state.busy || !this.state.loopKey) {
            return;
        }
        this.state.busy = true;
        try {
            const res = await this.api.agentTurn(this.state.loopKey, opts);
            this._apply(res);
        } catch (err) {
            this.notification.add(_t("智能体循环中断"), { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }

    /** Step forward — the operator's "continue". */
    step() {
        return this.advance({});
    }

    /** Answer a previous `ask_user`. Routed to the field the server named. */
    async submitAnswer() {
        const answer = (this.state.askAnswer || "").trim();
        if (!answer) {
            this.notification.add(_t("请输入内容"), { type: "warning" });
            return;
        }
        // Keep the text in the box: `_apply` clears it only once the
        // server confirms the question was consumed. A failed submit must
        // not silently eat what the operator just typed.
        await this.advance({ answer });
    }

    /**
     * Answer with one of the server's offered options (the scenario
     * question renders its candidates as buttons).
     *
     * The value sent is the candidate CODE, verbatim — the server
     * validates it against the candidate set it derived, and anything
     * outside that set is refused and the question re-asked. So this
     * method cannot widen anything: it can only pick from the list the
     * operator is looking at.
     */
    async submitOption(value) {
        if (!value) {
            return;
        }
        await this.advance({ answer: value });
    }

    /**
     * The human gate. This button exists ONLY while the server reports
     * `awaiting_confirmation`, i.e. the guard parked a write and is
     * waiting for a person. The server releases exactly that parked
     * request — this flag cannot authorize anything else.
     */
    confirm() {
        return this.advance({ confirmed: true });
    }

    /** Continue on the model path after the loop degraded to the recipe. */
    retry() {
        return this.advance({});
    }

    // ---------------------------------------------------------------- //
    // hands-free loop (FR-001)
    // ---------------------------------------------------------------- //

    /**
     * Drive the agent loop by voice.
     *
     * The agent already speaks its turns (`view.speak`). What was missing
     * is the way back: answering its question, approving its parked
     * write, and continuing after a degrade all required a click.
     */
    toggleHandsFree() {
        if (this.state.handsFree) {
            this._stopHandsFree();
            return;
        }
        this.state.voiceNotice = "";
        const ok = voiceLoop.start({
            context: "agent",
            onState: (s) => {
                this.state.loopState = s;
            },
            onWake: () => {
                this.state.voiceNotice = _t("已唤醒，请说");
            },
            onError: (msg) => {
                this.state.voiceNotice = msg;
                this.state.handsFree = false;
                this.state.loopState = LOOP_STATES.IDLE;
            },
            onTranscript: (ev) => this._onVoice(ev),
            onCapture: (b64, deliver) => this._transcribeCaptured(b64, deliver),
        });
        this.state.handsFree = ok;
        if (!ok) {
            this.state.voiceNotice = _t("此设备不支持免手模式，请使用屏幕操作。");
            return;
        }
        voiceLoop.setAllowConsent(this.isAwaitingConfirmation);
        this._speak(this.speakText || _t("免手模式已开启"));
    }

    _stopHandsFree() {
        voiceLoop.stop();
        this.state.handsFree = false;
        this.state.loopState = LOOP_STATES.IDLE;
        this.state.voiceNotice = "";
    }

    async _transcribeCaptured(b64, deliver) {
        try {
            const res = await this.api.transcribe(b64);
            deliver(res.error ? "" : res.text || "");
        } catch (err) {
            deliver("");
        }
    }

    async _onVoice({ text, kind }) {
        if (kind === "empty") {
            return;
        }
        if (kind === "repeat") {
            this._speak(this.speakText || this.pendingQuestion ||
                        _t("当前没有可复述的内容"));
            return;
        }
        if (kind === "confirm") {
            if (this.isAwaitingConfirmation) {
                await this.confirm();
                return;
            }
            this._speak(_t("当前没有待确认的操作"));
            return;
        }
        if (kind === "skip" || kind === "cancel") {
            // The agent's own way of declining is the recipe fallback, so
            // a refusal leaves the work with a working engine rather than
            // abandoning it.
            voiceLoop.setAllowConsent(false);
            await this.retry();
            return;
        }
        if (!this.isAwaitingUser) {
            // Not a question point: the loop moves on its own, so there is
            // nothing to answer. Say so rather than swallowing the words.
            this._speak(_t("现在不需要回答，请稍候"));
            return;
        }
        await this._answerByVoice(text);
    }

    /**
     * Answer the agent's question by voice.
     *
     * When the server offered options, the spoken words are matched
     * against the option LABELS, and the option's **code** is what gets
     * sent -- exactly what the button would have sent. The code is the
     * server's own value, so voice cannot widen the answer set; an
     * unmatched utterance falls through as free text, which the server
     * validates on its side just like typed input.
     */
    async _answerByVoice(text) {
        const opts = this.pendingOptions || [];
        if (opts.length) {
            const spoken = normalizePhrase(text);
            const exact = opts.find((o) => normalizePhrase(o.value) === spoken);
            const byLabel = opts.find((o) => {
                const label = normalizePhrase(o.label || "");
                return label && (spoken === label || spoken.includes(label));
            });
            const hit = exact || byLabel;
            if (hit) {
                await this.submitOption(hit.value);
                return;
            }
        }
        // free-form answer: the server decides whether it fits the question
        this.state.askAnswer = text;
        await this.submitAnswer();
    }

    /** Speak without the microphone transcribing us (echo suppression). */
    _speak(text, onEnd) {
        this.state.lastSpoken = text;
        speakGuarded(text, { enabled: this.state.handsFree, onEnd });
    }

    // ---------------------------------------------------------------- //
    // state folding
    // ---------------------------------------------------------------- //
    _apply(res) {
        if (!res) {
            return;
        }
        if (res.error) {
            this.state.error = res.error;
            this.state.errorCode = res.code || "";
            this.notification.add(res.error, { type: "warning" });
            return;
        }
        this.state.error = "";
        this.state.errorCode = "";
        if (res.loop_key) {
            this.state.loopKey = res.loop_key;
        }
        // Append the turn to the local trace. The trace is append-only:
        // the agent's reasoning is history, and history that can be
        // rewritten is not evidence.
        const entry = this._traceEntry(res);
        if (entry) {
            this.state.trace = [...this.state.trace, entry];
        }
        // Clear the answer box only when the question has actually been
        // consumed, so a failed submit does not silently eat the answer.
        if (!res.awaiting_user) {
            this.state.askAnswer = "";
        }
        this.state.view = res;

        // speak the turn: in the field the operator is usually
        // hands-busy and cannot read the screen
        if (this.state.handsFree) {
            // route through `_speak` so the microphone is muted while we
            // talk -- otherwise the loop transcribes its own prompt
            const line = res.speak
                || (res.awaiting_confirmation ? _t("需要确认后执行") : "");
            if (line) {
                this._speak(line);
            }
        } else if (res.speak) {
            tts.speak(res.speak);
        } else if (this.isAwaitingConfirmation && this.execConfirmation) {
            tts.speak(_t("需要确认后执行"));
        }

        // Consent is only meaningful while the guard has a write parked.
        voiceLoop.setAllowConsent(this.isAwaitingConfirmation);

        // A mid-loop step (an `answer` or a `propose_step`) is not a
        // place to stop and wait for a click — the agent is still
        // working. Pump that continuation on its own, but ONLY while the
        // loop is moving on its own: never across the confirmation gate
        // and never across an `ask_user`, because both of those exist
        // precisely to hand control back to a person.
        //
        // The pump is also strictly one step per timer tick, and it
        // re-reads `isParked` at fire time: a turn that parked while the
        // timer was queued must not be stepped over.
        if (res.state === "running" && !this.isParked && !this.state.autoPumped) {
            this.state.autoPumped = true;
            setTimeout(() => {
                this.state.autoPumped = false;
                if (this._alive && !this.isParked && !this.state.busy) {
                    this.advance({});
                }
            }, 0);
        }
    }

    _traceEntry(res) {
        const turn = res.turn;
        if (!turn) {
            return null;
        }
        return {
            turn: turn,
            action: turn.action,
            state: turn.state,
            source: turn.source,
            exec: turn.exec || res.exec || null,
            rejected_reason: turn.rejected_reason,
            reason: turn.rejected_reason || null,
        };
    }
}
