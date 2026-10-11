/** @odoo-module **/

import { Component, onWillUnmount, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { mmuxApi } from "../services/mmux_api";
import { tts } from "../services/tts";
import { speakGuarded } from "../services/speech";
import { LOOP_STATES, voiceLoop } from "../services/voice_loop";
import { MmuxGuide } from "../guide/guide";
import { MmuxAgentTrace } from "../agentloop/agent_trace";

/**
 * Task loop — the workflow the whole platform is built around:
 *
 *   get a task from the system -> do it (with the guided flow for
 *   help) -> report it done -> the system marks it complete and
 *   publishes the NEXT task -> repeat.
 *
 * The loop is server-driven: this component never decides what comes
 * next, it only renders `loop.state` and asks for the next task. The
 * cursor lives server-side (interaction log), so a page switch or a
 * reload resumes the same task.
 *
 * Asking for help mid-task is a first-class action: the backend answers
 * with either a guidance hint or a resolved capability (`action:
 * "execute"`), never a silent business write.
 */
export class MmuxTaskLoop extends Component {
    static components = { MmuxGuide, MmuxAgentTrace };
    static template = "mmux_core.TaskLoop";

    setup() {
        this.api = mmuxApi;
        this.notification = useService("notification");
        this.state = useState({
            scenario: "",
            sources: [],
            task: null,
            loop: null,
            loopKey: null,
            loopLabel: "",
            busy: false,
            // completion reporting
            report: "",
            confirmation: null,
            // guidance / help
            question: "",
            hint: "",
            hintKind: "",          // "guidance" | "execute" | "error"
            pendingIntent: null,
            // guided flow (bound to the current task)
            guide: null,
            showGuide: false,
            // who drives the guidance for this task: "agent" | "scripted"
            guideMode: "agent",
            // agent decision ("why this task now?")
            agent: null,
            showAlternatives: false,
            // hands-free loop
            handsFree: false,
            loopState: LOOP_STATES.IDLE,
            voiceNotice: "",
            lastSpoken: "",
        });
        onWillUnmount(() => {
            this._stopHandsFree();
            tts.stop();
        });
    }

    // ---------------------------------------------------------------- //
    // hands-free loop (FR-001)
    // ---------------------------------------------------------------- //

    /**
     * Drive the whole task loop by voice.
     *
     * This is the workflow the platform exists for, and until now it was
     * the one place that still required taps: getting the next task,
     * reporting completion, confirming the posting, asking for help. With
     * this on, all four are spoken.
     *
     * The most important property: **the closing half is automatic**. A
     * worker finishes a task by simply describing what they did -- the
     * report path is the ordinary content path, so there is no "now press
     * report" step.
     */
    toggleHandsFree() {
        if (this.state.handsFree) {
            this._stopHandsFree();
            return;
        }
        this.state.voiceNotice = "";
        const ok = voiceLoop.start({
            context: "taskloop",
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
        this._speak(_t("免手模式已开启。说「助手 下一个」取任务。"), () => {
            // auto-fetch the first task so the worker does not even have
            // to ask for one
            if (!this.state.task) {
                this.fetchNext();
            }
        });
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

    /**
     * Route one spoken utterance.
     *
     * Note what is NOT here: there is no "report" command. Saying what you
     * did IS the report. That is what makes the loop closable without a
     * screen -- once the worker stops talking, the sentence is submitted
     * to the same `/mmux/task/report` endpoint the button used.
     */
    async _onVoice({ text, kind }) {
        if (kind === "empty") {
            return;
        }
        if (kind === "repeat") {
            this._repeatCurrent();
            return;
        }
        if (kind === "next") {
            await this.fetchNext();
            return;
        }
        if (kind === "cancel") {
            this.state.confirmation = null;
            this.state.pendingIntent = null;
            voiceLoop.setAllowConsent(false);
            this._speak(_t("已取消"));
            return;
        }
        if (kind === "confirm") {
            if (this.state.confirmation) {
                await this.confirmReport();
                return;
            }
            this._speak(_t("当前没有待确认的操作"));
            return;
        }

        // A question is told apart from a completion report by its words,
        // not by a separate mode: the worker should not have to remember
        // to "enter help mode".
        const question = this._extractQuestion(text);
        if (question) {
            await this._askByVoice(question);
            return;
        }

        // ---- ordinary speech: this is the completion report ----
        if (!this.state.loopKey) {
            this._speak(_t("请先说「下一个」取一个任务"));
            return;
        }
        this.state.report = text;
        await this.submitReport(false);
    }

    /**
     * Pull the question out of an utterance, or return "" if it is not a
     * question.
     *
     * Recognised by the interrogative shape (a trailing question mark, or
     * a question word), because requiring a "help" keyword would make the
     * worker phrase a genuine question as a command. A statement about
     * completed work has no question mark and no interrogative, so it
     * falls through to the report path -- which is the common case and
     * must not be misread as a question.
     */
    _extractQuestion(text) {
        const t = (text || "").trim();
        if (!t) {
            return "";
        }
        if (/[?？]$/.test(t)) {
            return t;
        }
        const WORDS = ["怎么", "怎样", "为什么", "哪里", "在哪", "多少",
                       "是什么", "哪个", "能不能", "可不可以",
                       "how", "where", "what", "why", "which"];
        const low = t.toLowerCase();
        if (WORDS.some((w) => low.includes(w))) {
            return t;
        }
        return "";
    }

    /** Ask the question via voice and speak whatever comes back. */
    async _askByVoice(question) {
        this.state.question = question;
        await this.askHelp();
    }

    /** Re-read whatever the worker should hear right now. */
    _repeatCurrent() {
        if (this.state.confirmation) {
            const label = this.state.confirmation.label || "";
            this._speak(_t("等待确认：") + label +
                        _t("。说「确认」或「取消」。"));
            return;
        }
        if (this.state.task) {
            this._speak(this.state.task.title);
            return;
        }
        this._speak(_t("当前没有任务"));
    }

    /**
     * Speak without the microphone transcribing us.
     *
     * `onEnd` re-arms the loop, so the worker can always answer.
     */
    _speak(text, onEnd) {
        this.state.lastSpoken = text;
        speakGuarded(text, { onEnd });
    }

    // ---------------------------------------------------------------- //
    // task retrieval
    // ---------------------------------------------------------------- //
    async fetchNext() {
        this.state.busy = true;
        try {
            const res = await this.api.taskNext(this.state.scenario);
            this._applyNext(res);
            if (!res.task) {
                this.notification.add(
                    res.loop_label || _t("当前没有待办任务"), { type: "info" });
            }
        } catch (err) {
            this.notification.add(_t("获取任务失败"), { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }

    _applyNext(res) {
        this.state.task = res.task || null;
        this.state.loop = res.loop || null;
        this.state.loopKey = res.loop_key || res.next_loop_key || null;
        this.state.loopLabel = res.loop_label || "";
        this.state.sources = res.sources || this.state.sources;
        // the agent's justification for picking THIS task (may be null on
        // resume, or a fallback pick when the model was unavailable)
        this.state.agent = res.agent || null;
        this.state.showAlternatives = false;
        // a new task invalidates the previous report / help / guide
        this.state.report = "";
        this.state.confirmation = null;
        this.state.question = "";
        this.state.hint = "";
        this.state.hintKind = "";
        this.state.pendingIntent = null;
        this.state.guide = null;
        this.state.showGuide = false;
        if (this.state.task) {
            this._announceTask();
        }
    }

    /**
     * Read the task and the agent's reason aloud.
     *
     * In the field the operator is often hands-busy and cannot read the
     * screen, so the reason matters as much as the title: "why this task
     * now" is what lets them trust an autonomously picked task.
     */
    _announceTask() {
        if (!this.state.task) {
            return;
        }
        const title = _t("任务：") + this.state.task.title;
        const reason = (this.state.agent && this.state.agent.reason) || "";
        if (!this.state.handsFree) {
            tts.speak(this.state.task.title);
            if (reason) {
                tts.speak(reason);
            }
            return;
        }
        // one utterance, so the worker hears the whole thing without an
        // extra prompt in between
        this._speak(reason ? `${title}。${reason}` : title);
    }

    /** A new loop key changes the agent's mission; the mount follows. */
    get loopKeyForAgent() {
        return this.state.loopKey;
    }

    // ---------------------------------------------------------------- //
    // agent decision ("why this task now?")
    // ---------------------------------------------------------------- //
    get agentWasAutonomous() {
        return !!(this.state.agent && this.state.agent.source === "llm");
    }

    get agentIsFallback() {
        return !!(this.state.agent && this.state.agent.source !== "llm"
                  && this.state.agent.task_id);
    }

    get agentAlternatives() {
        return (this.state.agent && this.state.agent.alternatives) || [];
    }

    get agentConfidencePct() {
        const c = (this.state.agent && this.state.agent.confidence) || 0;
        return Math.round(Math.max(0, Math.min(1, c)) * 100);
    }

    toggleAlternatives() {
        this.state.showAlternatives = !this.state.showAlternatives;
    }

    async resumeLoop() {
        if (!this.state.loopKey) return;
        this.state.busy = true;
        try {
            const res = await this.api.taskCurrent(this.state.loopKey);
            if (res.error) {
                this.notification.add(res.error, { type: "warning" });
                return;
            }
            this._applyNext({ ...res });
        } finally {
            this.state.busy = false;
        }
    }

    get isFinishedAll() {
        return this.state.loop && this.state.loop.state === "finished_all";
    }

    // ---------------------------------------------------------------- //
    // report completion (the loop's closing half)
    // ---------------------------------------------------------------- //
    async submitReport(confirmed = false) {
        if (!this.state.loopKey) return;
        const text = (this.state.report || "").trim();
        // a confirmation round-trip may have no new text
        if (!text && !confirmed && !this.state.pendingIntent) {
            this.notification.add(_t("请描述完成情况"), { type: "warning" });
            return;
        }
        this.state.busy = true;
        try {
            const res = await this.api.taskReport(this.state.loopKey, {
                text: text || null,
                intent: this.state.pendingIntent,
                confirmed,
            });
            if (res.error && !res.state) {
                // structured error (e.g. llm_unavailable) — keep the text
                this.notification.add(res.error, { type: "warning" });
                return;
            }
            if (res.state === "awaiting_confirmation") {
                // L2 completion: park and ask the human, loop does not move
                this.state.confirmation = res.confirmation;
                this.state.loop = res.loop || this.state.loop;
                this.state.loopLabel = res.loop_label || this.state.loopLabel;
                this.state.pendingIntent =
                    (res.confirmation && res.confirmation.intent) || null;
                // The gate is open, so "确认" is now meaningful.
                voiceLoop.setAllowConsent(true);
                if (this.state.handsFree) {
                    const label =
                        (res.confirmation && res.confirmation.label) || "";
                    this._speak(_t("需要确认过账：") + label +
                                _t("。说「确认」过账，或「取消」。"));
                } else {
                    tts.speak(_t("请确认后过账"));
                }
                return;
            }
            if (res.state === "not_understood" || res.state === "task_mismatch") {
                this.state.hint = res.error || "";
                this.state.hintKind = "error";
                this.notification.add(res.error, { type: "warning" });
                this._speak(res.error || _t("没有听懂"));
                return;
            }
            if (res.state === "report_failed") {
                const msg = (res.exec && res.exec.error) || _t("汇报失败");
                this.notification.add(msg, { type: "danger" });
                this._speak(_t("汇报失败：") + msg);
                return;
            }
            // state === "completed": the system marked it done and either
            // published the next task or ended the loop
            this.state.confirmation = null;
            this.state.pendingIntent = null;
            // gate closed: "确认" reverts to ordinary speech so it cannot
            // pre-approve the next task's posting
            voiceLoop.setAllowConsent(false);
            this.notification.add(_t("任务已完成"), { type: "success" });
            this._applyNext(res);
            if (this.state.handsFree) {
                // one announcement that includes the next task, so the
                // worker is not left waiting after "done"
                if (this.state.task) {
                    this._announceTask();
                } else {
                    this._speak(_t("任务已完成，没有更多待办"));
                }
            } else {
                tts.speak(_t("任务已完成"));
                if (this.state.task) {
                    tts.speak(_t("下一个任务：") + this.state.task.title);
                }
            }
        } finally {
            this.state.busy = false;
        }
    }

    confirmReport() {
        return this.submitReport(true);
    }

    cancelConfirmation() {
        this.state.confirmation = null;
        this.state.pendingIntent = null;
        // close the consent gate so a later stray "确认" cannot post
        voiceLoop.setAllowConsent(false);
    }

    // ---------------------------------------------------------------- //
    // guided help for the current task
    // ---------------------------------------------------------------- //
    /**
     * Which engine drives the guidance.
     *
     * v3.0 is a *pure agent* architecture: the agent loop is the default
     * driver and the scripted flow is its fallback. The scripted engine
     * is genuinely kept, not just tolerated — it is what the agent itself
     * degrades to when no model is reachable, so the operator can always
     * ask for it explicitly too (field connectivity is not a given).
     *
     * Switching modes only changes WHO picks the next step. It cannot
     * change what is permitted: both paths call the same capabilities
     * through the same ExecutionGuard.
     */
    async setGuideMode(mode) {
        if (this.state.guideMode === mode) return;
        this.state.guideMode = mode;
        // an in-flight run belongs to the other engine; restart cleanly
        // under the new one rather than mixing two cursors
        this.state.guide = null;
        this.state.showGuide = false;
        tts.stop();
    }

    get guideIsAgent() {
        return this.state.guideMode === "agent";
    }

    async startGuide() {
        if (!this.state.loopKey) return;
        this.state.busy = true;
        try {
            const view = await this.api.taskGuide(
                this.state.loopKey, this.state.guideMode);
            if (view.error) {
                this.notification.add(view.error, { type: "warning" });
                return;
            }
            // the server tells us which engine actually ended up driving:
            // if the agent was enabled but could not start, it degrades to
            // the scripted flow. Report that honestly instead of showing an
            // agent badge over a scripted run.
            const actual = view.mode || this.state.guideMode;
            if (actual !== this.state.guideMode) {
                this.state.guideMode = actual;
                this.notification.add(
                    _t("智能体不可用，已切换到脚本化引导"), { type: "info" });
            }
            this.state.guide = view;
            this.state.showGuide = true;
        } finally {
            this.state.busy = false;
        }
    }

    closeGuide() {
        this.state.showGuide = false;
        tts.stop();
    }

    // ---------------------------------------------------------------- //
    // mid-task question ("这个库位在哪 / how many?")
    // ---------------------------------------------------------------- //
    async askHelp() {
        const text = (this.state.question || "").trim();
        if (!text || !this.state.loopKey) return;
        this.state.busy = true;
        try {
            const res = await this.api.taskHelp(this.state.loopKey, text);
            if (res.error && !res.action) {
                this.state.hint = res.error;
                this.state.hintKind = "error";
                return;
            }
            this.state.hint = res.hint || "";
            if (res.action === "execute") {
                // the question resolved to a whitelisted capability: run it
                // through the SAME funnel (/mmux/execute) — no shortcut
                this.state.hintKind = "execute";
                this.state.pendingIntent = res.intent || null;
                const cap = res.intent && res.intent.capability_id;
                const params = (res.intent && res.intent.params) || {};
                const exec = await this.api.execute(
                    cap, params, this.state.task && this.state.task.res_id,
                    this.api.newIdempotencyKey(), false);
                if (exec.status === "awaiting_confirmation") {
                    this.state.confirmation = exec.confirmation;
                    voiceLoop.setAllowConsent(true);
                    if (this.state.handsFree) {
                        this._speak(_t("需要确认：") +
                                    ((exec.confirmation && exec.confirmation.label) || "") +
                                    _t("。说「确认」或「取消」。"));
                    }
                } else if (exec.status === "success") {
                    this.notification.add(_t("已执行"), { type: "success" });
                    this._speak(_t("已执行"));
                } else if (exec.error) {
                    this.notification.add(exec.error, { type: "warning" });
                    this._speak(exec.error);
                }
            } else {
                this.state.hintKind = "guidance";
                if (this.state.hint) {
                    this._speak(this.state.hint);
                }
            }
        } finally {
            this.state.busy = false;
        }
    }
}
