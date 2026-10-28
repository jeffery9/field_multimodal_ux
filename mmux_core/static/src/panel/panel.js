/**
 * Copyright (C) 2026 jeffery9@gmail.com & geninit
 * License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
 */
/** @odoo-module **/

import { Component, useState, onWillStart, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { mmuxApi, STATUS_LABELS } from "../services/mmux_api";
import { tts } from "../services/tts";
import { speakGuarded } from "../services/speech";
import { LOOP_STATES, voiceLoop } from "../services/voice_loop";
import { MmuxCapture } from "../capture/camera_capture";
import { captureNotice, photosParam, recognizePhoto } from "../capture/capture_service";
import { pickQuantity } from "../capture/capture_logic";

/**
 * The multimodal panel (FR-010/FR-011): voice, barcode, camera and text
 * inputs -> intent card -> risk-based confirmation -> execution feedback.
 * Degrades gracefully: mic/camera permission denial or provider failure
 * falls back to plain text input (FR-013, PRD §2.6).
 */
export class MmuxPanel extends Component {
    static components = { MmuxCapture };

    setup() {
        this.api = mmuxApi;
        this.contextSvc = useService("mmux_context");
        this.notification = useService("notification");
        this.state = useState({
            open: false,
            scenario: this.props.scenario || "",
            capabilities: [],
            transcript: "",
            text: "",
            recording: false,
            mediaError: "",
            barcode: "",
            // device capture: null | "scan" | "ocr" | "evidence"
            capture: null,
            photos: [], // evidence photos, kept until the intent executes
            captureNotice: "",
            intent: null,
            // LLM-first recognition outcome:
            //   ""            -> not parsed yet
            //   "empty"       -> parsed OK but matched no capability
            //   "unavailable" -> LLM path down (retryable?)
            parseState: "",
            parseError: null,
            resolving: false,
            candidates: [],
            selectedRecordId: null,
            confirming: null, // confirmation payload
            result: null, // {status, result|error}
            idemKey: null,
            busy: false,
            // hands-free loop (voice in, voice out, no taps)
            handsFree: false,
            loopState: LOOP_STATES.IDLE,
            voiceNotice: "",
            lastSpoken: "",
            // plan orchestration (agent loop) state
            plan: null,
            planRun: null,
            planInputs: {},
        });
        onWillStart(() => this._loadCapabilities());
        onWillUnmount(() => {
            this._stopHandsFree();
            tts.stop();
        });
    }

    async _loadCapabilities() {
        try {
            this.state.capabilities = await this.api.capabilities(this.state.scenario);
        } catch (e) {
            this.state.capabilities = [];
        }
    }

    get context() {
        return this.contextSvc.getContext();
    }

    // ---------------- voice (FR-001) ---------------- #
    async toggleRecording() {
        if (this.state.recording) {
            this.mediaRecorder && this.mediaRecorder.stop();
            return;
        }
        this.state.mediaError = "";
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            this._chunks = [];
            const mr = new MediaRecorder(stream);
            mr.ondataavailable = (ev) => this._chunks.push(ev.data);
            mr.onstop = async () => {
                stream.getTracks().forEach((t) => t.stop());
                this.state.recording = false;
                await this._transcribe(new Blob(this._chunks));
            };
            this.mediaRecorder = mr;
            mr.start();
            this.state.recording = true;
        } catch (err) {
            // permission denied or no mic: explicit fallback to text (FR-001)
            this.state.mediaError = _t("Microphone unavailable — use text input.");
        }
    }

    async _transcribe(blob) {
        this.state.busy = true;
        try {
            const b64 = await this._blobToB64(blob);
            const res = await this.api.transcribe(b64);
            if (res.error) {
                this.state.mediaError = _t("Transcription unavailable — use text input.");
            } else {
                this.state.transcript = res.text || "";
                this.state.text = this.state.transcript;
                await this.parseIntent();
            }
        } finally {
            this.state.busy = false;
        }
    }

    _blobToB64(blob) {
        return new Promise((resolve, reject) => {
            const fr = new FileReader();
            fr.onload = () => resolve(fr.result.split(",")[1]);
            fr.onerror = reject;
            fr.readAsDataURL(blob);
        });
    }

    // ---------------- hands-free loop (FR-001) ---------------- #

    /**
     * Turn the microphone loop on/off.
     *
     * This is the switch that makes the panel usable without looking at
     * it: with it on, the worker says the wake word, dictates, and
     * confirms high-risk steps by voice. Every existing on-screen control
     * stays exactly as it was, so this is additive -- a device without
     * speech recognition keeps working the old way.
     */
    toggleHandsFree() {
        if (this.state.handsFree) {
            this._stopHandsFree();
        } else {
            this._startHandsFree();
        }
    }

    _startHandsFree() {
        this.state.voiceNotice = "";
        const ok = voiceLoop.start({
            context: "panel",
            onState: (s) => {
                this.state.loopState = s;
            },
            onWake: () => {
                // Audible acknowledgement: the worker cannot see a spinner
                // from across the aisle, so the beep-in-words matters.
                this.state.voiceNotice = _t("已唤醒，请说指令");
                tts.speak(_t("请说"));
            },
            onError: (msg) => {
                this.state.voiceNotice = msg;
                this.state.handsFree = false;
                this.state.loopState = LOOP_STATES.IDLE;
            },
            onTranscript: (ev) => this._onVoiceTranscript(ev),
            onCapture: (b64, deliver) => this._transcribeCaptured(b64, deliver),
        });
        this.state.handsFree = ok;
        if (!ok) {
            this.state.voiceNotice = _t("此设备不支持免手模式，请使用屏幕操作。");
            return;
        }
        // Speak the instruction while NOT capturing, to avoid the
        // loudspeaker feeding back into the microphone.
        speakGuarded(_t("免手模式已开启，说「助手」开始"));
    }

    _stopHandsFree() {
        voiceLoop.stop();
        this.state.handsFree = false;
        this.state.loopState = LOOP_STATES.IDLE;
        this.state.voiceNotice = "";
    }

    /** Transcribe a captured clip, then hand the text back to the loop. */
    async _transcribeCaptured(b64, deliver) {
        try {
            const res = await this.api.transcribe(b64);
            if (res.error) {
                this.state.voiceNotice = _t("转写不可用，请改用文字输入。");
                deliver("");
                return;
            }
            deliver(res.text || "");
        } catch (err) {
            deliver("");
        }
    }

    /**
     * The single place voice drives the panel.
     *
     * `kind` comes from `classifyUtterance`, which only reports "confirm"
     * when the uttance IS an affirmative AND a confirmation is pending --
     * a stray "确认" during normal work is content, never consent.
     */
    async _onVoiceTranscript({ text, kind }) {
        if (kind === "empty") {
            return;
        }
        this.state.transcript = text;

        if (kind === "repeat") {
            // The worker missed what was said; re-speak the current state
            // instead of making them ask again.
            if (this.state.confirming) {
                this._announceResultByVoice();
            } else if (this.state.lastSpoken) {
                this._speak(this.state.lastSpoken);
            } else {
                this._speak(_t("还没有可以复述的内容"));
            }
            return;
        }
        if (kind === "cancel") {
            this.state.confirming = null;
            this.state.planInputs = {};
            // Close the consent gate too: leaving it open would let a
            // later stray "确认" approve whatever comes next.
            voiceLoop.setAllowConsent(false);
            this._speak(_t("已取消"));
            return;
        }
        if (kind === "confirm") {
            this._speak(_t("确认执行"));
            await this._confirmByVoice();
            return;
        }
        // ordinary instruction: parse it and show the intent card
        this.state.text = text;
        await this.parseIntent();
        this._announceIntentByVoice();
    }

    /** Route a spoken "确认" to whatever is actually awaiting consent. */
    async _confirmByVoice() {
        if (this.state.confirming) {
            // `execute()` announces the outcome itself.
            await this.execute(true);
            return;
        }
        if (this.state.planRun && this.state.planRun.status === "awaiting_confirmation") {
            await this.runPlan(this.state.planRun.pending_step || null);
            this._announceResultByVoice();
            return;
        }
        // Nothing was pending: the words were just speech.
        this._speak(_t("当前没有待确认的操作"));
    }

    /**
     * Read the intent card aloud.
     *
     * A hands-free user cannot see the card, so the risk level and the
     * target have to be spoken -- otherwise they would have to guess what
     * "确认" is about to do.
     */
    _announceIntentByVoice() {
        const intent = this.state.intent;
        if (!intent || !intent.capability_id) {
            const why = this.state.parseState === "unavailable"
                ? _t("识别服务不可用，请再说一次")
                : _t("没有听懂，请再说一次");
            this._speak(why);
            return;
        }
        const label = intent.label || intent.capability_id;
        this._speak(_t("准备执行：") + label);
    }

    _announceResultByVoice() {
        const res = this.state.result;
        if (!res) {
            return;
        }
        if (res.status === "awaiting_confirmation") {
            const c = res.confirmation || {};
            // Speak the risk explicitly: consent without knowing what is
            // being confirmed is not consent.
            this._speak(_t("此操作需要确认：") + (c.label || "") + _t("。说「确认」执行，或「取消」。"));
            voiceLoop.setAllowConsent(true);
            return;
        }
        voiceLoop.setAllowConsent(false);
        if (res.status === "success") {
            this._speak(_t("已完成"));
        } else if (res.status === "unknown") {
            this._speak(_t("结果未知，请核对记录后再试。"));
        } else {
            this._speak(_t("执行失败"));
        }
    }

    /** Speak without the microphone hearing us (echo suppression). */
    _speak(text, onEnd) {
        this.state.lastSpoken = text;
        speakGuarded(text, { enabled: this.state.handsFree, onEnd });
    }

    // ---------------- device capture (FR-003 / PRD §2.6) ---------------- #

    /**
     * Open the camera.
     *
     *   "scan"     -> decode a barcode into `state.barcode`
     *   "ocr"      -> photograph text, recognize it into `state.text`
     *   "evidence" -> photograph the job, carry it as `photos` for the
     *                 intent's parameter
     *
     * All three hand the voice loop off and take it back on close, which
     * is what the panel needs when it is running hands-free: the worker
     * taps once, the mic is deaf while the camera is up, and the
     * conversation resumes exactly where it was.
     */
    openCapture(kind) {
        this.state.captureNotice = "";
        this.state.capture = kind;
    }

    closeCapture() {
        this.state.capture = null;
    }

    /** A decoded barcode: fill the box, then resolve it as usual. */
    async onCapturedBarcode(code) {
        this.state.capture = null;
        this.state.barcode = code;
        await this.resolveBarcode(code);
    }

    /**
     * A photo. What it becomes depends on why the camera was opened.
     *
     * Note what happens in every branch: the result is FILLED, never
     * submitted. Recognition can misread -- a wrong value typed by the
     * worker is a mistake they can see; a wrong value auto-submitted is
     * a business write nobody authorized.
     */
    async onCapturedPhoto(b64) {
        const kind = this.state.capture;
        this.state.capture = null;
        if (kind === "evidence") {
            this.state.photos = this.state.photos.concat([
                { image_b64: String(b64).split(",").pop(), name: "field_photo" },
            ]);
            this.state.captureNotice = `已拍下 ${this.state.photos.length} 张照片，执行时作为记录附件留存`;
            this._speak(this.state.captureNotice);
            return;
        }
        // OCR: read what is in the picture and put it where the worker
        // can look at it before deciding.
        this.state.busy = true;
        this.state.captureNotice = "正在识别…";
        try {
            const text = await recognizePhoto(b64, "ocr");
            if (text == null) {
                this.state.captureNotice = captureNotice("text", null).text;
                this._speak(this.state.captureNotice);
                return;
            }
            // A photo of a label is usually a quantity or a code. Put the
            // read value in the barcode box when it looks like a code, so
            // the existing resolve path runs; otherwise in the draft box.
            const looksLikeCode = /^[\dA-Za-z\-]{6,}$/.test(text.replace(/\s/g, ""));
            if (looksLikeCode && !pickQuantity(text)) {
                this.state.barcode = text.replace(/\s/g, "");
                this.state.captureNotice = `识别到：${text}（可直接回车查库）`;
                this._speak(this.state.captureNotice);
            } else {
                this.state.text = this.state.text
                    ? `${this.state.text} ${text}`
                    : text;
                this.state.captureNotice = `已识别：${text}，请核对后解析`;
                this._speak(this.state.captureNotice);
            }
        } finally {
            this.state.busy = false;
        }
    }

    removePhoto(index) {
        this.state.photos = this.state.photos.filter((_, i) => i !== index);
        if (!this.state.photos.length) {
            this.state.captureNotice = "";
        }
    }

    /** The `photos` parameter, in the shape `ir.attachment` expects. */
    get photosParam() {
        return photosParam(this.state.photos);
    }

    // ---------------- barcode (FR-003) ---------------- #
    onBarcodeInput(ev) {
        this.state.barcode = ev.target.value;
    }

    async onScan(ev) {
        const barcode = (this.state.barcode || "").trim();
        if (!barcode) return;
        await this.resolveBarcode(barcode);
    }

    async resolveBarcode(barcode) {
        this.state.busy = true;
        try {
            const res = await this.api.resolve(barcode);
            this.state.candidates = res.candidates || [];
            if (this.state.candidates.length === 1) {
                this._applyCandidate(this.state.candidates[0]);
            }
        } finally {
            this.state.busy = false;
        }
    }

    _applyCandidate(cand) {
        this.state.selectedRecordId = cand.id;
        this.state.selectedModel = cand.model;
        this.state.text = this.state.text || `${cand.display_name}`;
    }
    // ---------------- intent (FR-005) ---------------- #
    async parseIntent() {
        const text = this.state.text.trim();
        if (!text) return;
        this.state.busy = true;
        this.state.intent = null;
        this.state.result = null;
        this.state.parseState = "";
        this.state.parseError = null;
        // A fresh parse is a fresh intent: a candidate picked for the
        // previous one must not silently carry over as this one's target.
        this.state.candidates = [];
        this.state.selectedRecordId = null;
        try {
            const intent = await this.api.parseIntent(text, this.context);
            // LLM path unavailable (not configured / timeout / invalid JSON)
            if (intent && intent.code === "llm_unavailable") {
                this.state.parseState = "unavailable";
                this.state.parseError = intent;
                return;
            }
            // parsed fine but no capability matched -> empty intent
            if (!intent || !intent.capability_id) {
                this.state.parseState = "empty";
                this.state.intent = intent || null;
                return;
            }
            this.state.intent = intent;
            this.state.idemKey = this.api.newIdempotencyKey();
        } catch (e) {
            this.state.parseState = "unavailable";
            this.state.parseError = { error: String(e), retryable: true };
        } finally {
            this.state.busy = false;
        }
    }

    setParam(ev, name) {
        this.state.intent.params[name] = ev.target.value;
    }

    // ---------------- execution (FR-016/FR-017) ---------------- #
    async execute(confirmed = false) {
        const intent = this.state.intent;
        if (!intent || !intent.capability_id) return;
        this.state.busy = true;
        try {
            // Evidence photos ride along as an ordinary parameter, so they
            // still travel the single `/mmux/execute` funnel -- the guard
            // keeps its ACL, record-rule and audit steps over them.
            const params = Object.assign({}, intent.params || {});
            const photos = this.photosParam;
            if (photos.length) {
                params.photos = photos;
            }
            const res = await this.api.execute(
                intent.capability_id,
                params,
                this.state.selectedRecordId,
                this.state.idemKey,
                confirmed
            );
            this.state.result = res;
            if (res.status === "awaiting_confirmation") {
                this.state.confirming = res.confirmation;
                // Consent words are now meaningful.
                voiceLoop.setAllowConsent(true);
            } else if (res.status === "success" || res.status === "failed") {
                this.state.confirming = null;
                // Nothing is pending any more, so "确认" reverts to being
                // ordinary speech and cannot pre-approve a later step.
                voiceLoop.setAllowConsent(false);
                if (res.status === "success") {
                    // The photos are on the record now; holding them would
                    // attach them a second time on the next execute.
                    this.state.photos = [];
                    this.state.captureNotice = "";
                    this.notification.add(_t("Operation executed"), { type: "success" });
                }
            } else if (res.status === "unknown") {
                voiceLoop.setAllowConsent(false);
                this.notification.add(
                    _t("Result unknown. Verify the record before retrying."),
                    { type: "warning" }
                );
            }
            if (this.state.handsFree) {
                this._announceResultByVoice();
            }
        } finally {
            this.state.busy = false;
        }
    }

    get statusLabels() {
        return STATUS_LABELS;
    }

    // ------------- plan orchestration (agent loop) ------------- #
    async proposePlan() {
        const text = this.state.text.trim();
        if (!text) return;
        this.state.busy = true;
        this.state.plan = null;
        this.state.planRun = null;
        try {
            const res = await this.api.planPropose(text, this.context);
            if (res.error) {
                this.notification.add(res.error, { type: "warning" });
            } else {
                this.state.plan = res.plan;
                this.state.idemKey = this.api.newIdempotencyKey();
            }
        } finally {
            this.state.busy = false;
        }
    }

    setPlanInput(ev, name) {
        this.state.planInputs[name] = ev.target.value;
    }

    async runPlan(confirmedStep = null) {
        if (!this.state.plan) return;
        this.state.busy = true;
        try {
            const confirmed = confirmedStep
                ? [...(this.state.planRun?.confirmed_steps || []), confirmedStep]
                : (this.state.planRun?.confirmed_steps || []);
            const res = await this.api.planRun({
                plan_id: this.state.plan.plan_id,
                inputs: this.state.planInputs,
                idempotency_key: this.state.idemKey,
                confirmed_steps: confirmed,
                context_record: this.context.res_model
                    ? { res_model: this.context.res_model, res_id: this.context.res_id }
                    : null,
            });
            res.confirmed_steps = confirmed;
            this.state.planRun = res;
        } finally {
            this.state.busy = false;
        }
    }
}
MmuxPanel.template = "mmux_core.Panel";
