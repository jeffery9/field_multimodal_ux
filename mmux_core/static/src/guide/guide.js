/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { mmuxApi } from "../services/mmux_api";
import { tts } from "../services/tts";
import { speakGuarded } from "../services/speech";
import { LOOP_STATES, voiceLoop } from "../services/voice_loop";
import { MmuxCapture } from "../capture/camera_capture";
import { captureNotice, photosParam, recognizeForStep } from "../capture/capture_service";
import {
    captureIntentFor,
    stepFieldFor,
    valueForField,
} from "../capture/capture_intent";

/**
 * Voice-guided task navigation (warehouse picking etc.).
 *
 * Loop: the backend speaks the current step's prompt (TTS) → the user
 * scans / dictates the quantity / confirms → guideAdvance executes the
 * step's capability through the normal guard → next prompt. Steps with
 * an L2 capability (e.g. picking validation) pause for explicit
 * confirmation, exactly like manual mode.
 */
export class MmuxGuide extends Component {
    static components = { MmuxCapture };

    setup() {
        this.api = mmuxApi;
        this.notification = useService("notification");
        this.state = useState({
            flows: [],
            flowId: "",
            guide: null, // current guide view {current, steps, guide_key,...}
            busy: false,
            voice: tts.enabled,
            qtyInput: "",
            scanInput: "",
            confirmation: null,
            skillLevel: "novice",   // novice = detailed, expert = terse
            // device capture: null | "scan" | "ocr" | "evidence"
            capture: null,
            photos: [],
            captureNotice: "",
            // hands-free loop inside the guided flow
            handsFree: false,
            loopState: LOOP_STATES.IDLE,
            voiceNotice: "",
            lastSpoken: "",
        });
        onWillStart(() => this._loadFlows());
        onWillUnmount(() => {
            this._stopHandsFree();
            tts.stop();
        });
    }

    async _loadFlows() {
        try {
            const res = await this.api.guideFlows();
            this.state.flows = res.flows || [];
            if (this.state.flows.length && this.state.flows[0].skill_level) {
                this.state.skillLevel = this.state.flows[0].skill_level;
            }
            // TTS voice follows the user's language (backend-resolved)
            if (this.state.flows.length && this.state.flows[0].lang) {
                tts.setLang(this.state.flows[0].lang.replace("_", "-"));
            }
        } catch (err) {
            this.state.flows = [];
        }
    }

    /** Toggle novice/expert. Affects step granularity only, never
     * permissions; an in-flight run restarts under the new level. */
    async setSkill(level) {
        this.state.skillLevel = level;
        try {
            await this.api.guideSkillLevel(level);
        } catch (err) {
            this.notification.add(_t("无法保存技能等级偏好"), { type: "warning" });
        }
    }

    get isNovice() {
        return this.state.skillLevel === "novice";
    }

    toggleVoice() {
        this.state.voice = !this.state.voice;
        if (!this.state.voice) {
            tts.stop();
        } else if (this.state.guide?.current) {
            this._speakCurrent();
        }
    }

    _speakCurrent() {
        const cur = this.state.guide && this.state.guide.current;
        if (cur && this.state.voice) {
            const text = cur.voice_prompt || cur.title;
            this.state.lastSpoken = text;
            // Speak without the microphone hearing us: pausing the loop
            // first is what stops it transcribing its own prompt.
            speakGuarded(text, { enabled: this.state.handsFree });
        }
    }

    // ---------------- hands-free loop (FR-001) ---------------- #

    /**
     * Drive the guided flow entirely by voice.
     *
     * The flow already speaks each step; what was missing is the way back.
     * With this on, the worker answers the prompt by speaking -- a barcode
     * becomes dictated digits, a quantity becomes a number, and an L2 step
     * is approved with "确认".
     */
    toggleHandsFree() {
        if (this.state.handsFree) {
            this._stopHandsFree();
            return;
        }
        this.state.voiceNotice = "";
        if (!this.state.voice) {
            this.state.voice = true;
        }
        const ok = voiceLoop.start({
            context: "guide",
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
        speakGuarded(_t("免手引导已开启，说「助手」开始"));
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
     * Map a spoken answer onto the current step's input type.
     *
     * What counts as a valid answer is decided by the SERVER's step
     * definition (`input_type`), not here -- this only turns speech into
     * the shape the step expects. An unrecognised value is reported back
     * by voice instead of being silently dropped.
     */
    async _onVoice({ text, kind }) {
        const cur = this.state.guide && this.state.guide.current;
        if (kind === "empty") {
            return;
        }
        if (kind === "repeat") {
            // Re-read the current step rather than making the worker ask.
            this._speakCurrent();
            return;
        }
        if (kind === "skip") {
            await this.skipStep();
            return;
        }
        if (kind === "cancel") {
            this.state.confirmation = null;
            // Close the consent gate too, so a later stray "确认" cannot
            // approve the following step.
            voiceLoop.setAllowConsent(false);
            this._speak(_t("已取消"));
            return;
        }
        if (kind === "confirm") {
            if (this.state.confirmation) {
                await this.confirmCapabilityStep();
                return;
            }
            this._speak(_t("当前没有待确认的步骤"));
            return;
        }
        if (!cur) {
            this._speak(_t("请先开始一个流程"));
            return;
        }

        const spoken = text.trim();
        if (cur.input_type === "voice_qty" || cur.input_type === "qty") {
            const qty = this._parseSpokenNumber(spoken);
            if (qty === null) {
                this._speak(_t("没听清数量，请再说一次"));
                return;
            }
            this.state.qtyInput = String(qty);
            await this.submitQty(false);
            return;
        }
        if (cur.input_type === "scan") {
            // Barcodes are dictated digit by digit; strip the separators
            // speech recognition likes to insert.
            const code = spoken.replace(/[\s\-_,.]/g, "");
            if (!code) {
                this._speak(_t("没听清条码，请再说一次"));
                return;
            }
            this.state.scanInput = code;
            await this.submitScan();
            return;
        }
        if (cur.input_type === "confirm") {
            await this.confirmStep();
            return;
        }
        // Free-form step: pass the words through as the step's input.
        await this._advance({ text: spoken });
    }

    /**
     * Read a number out of speech, including the Chinese forms a worker
     * actually uses ("十二", "一百二", "3 件").
     *
     * Returns null when nothing numeric is heard, so the caller can ask
     * again rather than submitting a wrong quantity into an inventory move.
     */
    _parseSpokenNumber(text) {
        const digits = text.replace(/[^\d.]/g, "");
        if (digits && !isNaN(parseFloat(digits))) {
            return parseFloat(digits);
        }
        // Chinese numerals, most specific pattern first.
        const CN = { 零: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5,
                     六: 6, 七: 7, 八: 8, 九: 9 };
        const m = text.match(/[零一二两三四五六七八九十百千]+/);
        if (!m) {
            return null;
        }
        const s = m[0];
        if (s === "十") {
            return 10;
        }
        let total = 0;
        let section = 0;
        let number = 0;
        for (const ch of s) {
            if (ch in CN) {
                number = CN[ch];
            } else if (ch === "十") {
                section += (number || 1) * 10;
                number = 0;
            } else if (ch === "百") {
                section += (number || 1) * 100;
                number = 0;
            } else if (ch === "千") {
                section += (number || 1) * 1000;
                number = 0;
            }
        }
        total += section + number;
        return total || null;
    }

    // ---------------- device capture (PRD §2.6) ---------------- #

    /**
     * Which camera the current step should offer, if any.
     *
     * Derived from the SERVER's `input_type` through the shared table, so
     * a decision step (`confirm`, `capability`) offers no camera here for
     * the same reason it offers none in the panel: photographing a step
     * must never be a way to reach its answer.
     */
    get captureIntent() {
        const cur = this.state.guide && this.state.guide.current;
        return cur ? captureIntentFor(cur.input_type) : null;
    }

    openCapture(kind) {
        this.state.captureNotice = "";
        this.state.capture = kind;
    }

    closeCapture() {
        this.state.capture = null;
    }

    /** A scanned barcode is the step's answer -- fill, then advance. */
    async onCapturedBarcode(code) {
        this.state.capture = null;
        this.state.scanInput = code;
        await this.submitScan();
    }

    /**
     * A captured photo.
     *
     * Two destinations, and the step decides which:
     *
     *   evidence -> kept as `photos` and attached when the step's
     *               capability executes;
     *   reading  -> OCR'd, then the value fills the step's own field and
     *               the step is submitted exactly as if it had been typed.
     *
     * The value is only ever put where the step already has a field. An
     * OCR result for a step with no text field is refused rather than
     * passed to `_advance`, because `_advance` is what writes.
     */
    async onCapturedPhoto(b64) {
        const cur = this.state.guide && this.state.guide.current;
        const kind = this.state.capture;
        this.state.capture = null;
        if (!cur) {
            return;
        }
        if (kind === "evidence") {
            this.state.photos = this.state.photos.concat([
                { image_b64: String(b64).split(",").pop(), name: "step_photo" },
            ]);
            this.state.captureNotice =
                `已拍下 ${this.state.photos.length} 张，提交本步骤时作为附件留存`;
            this._speak(this.state.captureNotice);
            return;
        }
        this.state.busy = true;
        this.state.captureNotice = "正在识别…";
        try {
            const { text, quantity } = await recognizeForStep(
                b64, cur.input_type);
            const field = stepFieldFor(cur.input_type);
            const value = valueForField(field, text, quantity);
            if (value == null) {
                this.state.captureNotice = captureNotice(field, null).text;
                this._speak(this.state.captureNotice);
                return;
            }
            this.state.captureNotice = captureNotice(field, value).text;
            this._speak(this.state.captureNotice);
            if (field === "quantity") {
                this.state.qtyInput = String(value);
                await this.submitQty(false);
            } else if (field === "barcode") {
                this.state.scanInput = String(value);
                await this.submitScan();
            } else if (field === "text") {
                await this._advance({ text: String(value) });
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

    /** Speak without the microphone hearing us (echo suppression). */
    _speak(text, onEnd) {
        this.state.lastSpoken = text;
        speakGuarded(text, { enabled: this.state.handsFree, onEnd });
    }

    async startFlow() {
        if (!this.state.flowId) return;
        this.state.busy = true;
        try {
            const view = await this.api.guideStart(this.state.flowId);
            if (view.error) {
                this.notification.add(view.error, { type: "danger" });
                return;
            }
            this.state.guide = view;
            this.state.confirmation = null;
            this._speakCurrent();
        } finally {
            this.state.busy = false;
        }
    }

    async resumeFlow() {
        const key = this.state.guide && this.state.guide.guide_key;
        if (!key) return;
        this.state.busy = true;
        try {
            const view = await this.api.guideCurrent(key);
            if (!view.error) {
                this.state.guide = view;
                this._speakCurrent();
            }
        } finally {
            this.state.busy = false;
        }
    }

    async _advance(inputData = null, skip = false) {
        const g = this.state.guide;
        if (!g || !g.current) return;
        this.state.busy = true;
        try {
            // Evidence photos are folded into the step's own input, so
            // they travel the ordinary `guideAdvance` -> capability ->
            // guard path. There is no second upload route.
            let data = inputData;
            if (this.state.photos.length) {
                data = Object.assign({}, inputData || {});
                data.photos = photosParam(this.state.photos);
            }
            const res = await this.api.guideAdvance(
                g.guide_key, g.current.step_id, data, skip);
            if (res.error) {
                this.notification.add(res.error, { type: "warning" });
                return;
            }
            if (res.state === "awaiting_confirmation") {
                this.state.confirmation = res.confirmation;
                // A step is now open, so "确认" is meaningful.
                voiceLoop.setAllowConsent(true);
                if (this.state.handsFree) {
                    const label = (res.confirmation && res.confirmation.label) || "";
                    this._speak(_t("此步骤需要确认：") + label +
                                _t("。说「确认」执行，或「取消」。"));
                }
                return;
            }
            if (res.state === "step_failed") {
                const msg = (res.exec && res.exec.error) || _t("step failed");
                this.notification.add(msg, { type: "danger" });
                if (this.state.handsFree) {
                    this._speak(_t("步骤失败：") + msg);
                }
                return;
            }
            this.state.guide = res;
            this.state.confirmation = null;
            // The gate closed: "确认" reverts to ordinary speech so it
            // cannot pre-approve whatever step comes next.
            voiceLoop.setAllowConsent(false);
            this.state.qtyInput = "";
            this.state.scanInput = "";
            // The step that carried them has been posted; holding them
            // would attach the same photos to the following step.
            this.state.photos = [];
            this.state.captureNotice = "";
            if (res.finished) {
                this.notification.add(_t("流程完成"), { type: "success" });
                this._speak(_t("全部步骤完成"));
            } else {
                this._speakCurrent();
            }
        } finally {
            this.state.busy = false;
        }
    }

    confirmStep() {
        return this._advance({});
    }

    skipStep() {
        return this._advance(null, true);
    }

    async submitScan() {
        const barcode = this.state.scanInput.trim();
        if (!barcode) return;
        await this._advance({ barcode });
    }

    async submitQty(confirmed = false) {
        const qty = parseFloat(this.state.qtyInput);
        if (isNaN(qty) || qty < 0) {
            this.notification.add(_t("请输入有效数量"), { type: "warning" });
            return;
        }
        await this._advance({ quantity: qty, confirmed });
    }

    async confirmCapabilityStep() {
        // L2 step inside the guide: explicit confirmation, same guard
        await this._advance({ confirmed: true });
    }
}
MmuxGuide.template = "mmux_core.Guide";
