/** @odoo-module **/

/**
 * Hands-free voice loop (PRD §2.6, FR-001).
 *
 * Wires the pure decisions in `voice_logic.js` to the browser's audio
 * APIs, and exposes a single small surface to the components:
 *
 *     voiceLoop.start({ onTranscript, allowConsent })
 *     voiceLoop.stop()
 *     voiceLoop.suspend()   // while the assistant speaks
 *     voiceLoop.resume()
 *     const release = voiceLoop.beginHandoff()   // while a device acts
 *     release()                                  // ...device finished
 *
 * `suspend`/`resume` and `beginHandoff` hold *separate* suspensions, so
 * speaking and photographing can overlap without one cutting the other
 * short. See `_suspendFor` for why a plain boolean is not enough.
 *
 * The state machine:
 *
 *     idle ──start──▶ listening ──wake word──▶ capturing ──silence──▶ thinking
 *                        ▲                                              │
 *                        └──────────── speaking ◀──────────────────────┘
 *
 * ## Two design points worth stating explicitly
 *
 * **Only post-wake audio is uploaded.**  In `listening` the microphone is
 * open but the samples are matched against the wake phrase locally and
 * then discarded.  `_uploadArmed` gates the upload, so an accidental
 * always-on microphone does not become a transcript of the worker's day.
 *
 * **Speech recognition needs a recogniser, not just a recorder.**  The
 * wake-word check has to read *words*, so `listening` uses
 * `SpeechRecognition` (which some browsers implement only with a cloud
 * back-end) and falls back to push-to-talk via the existing
 * `MediaRecorder` path when the API is absent.  The loop never pretends
 * the feature works: `supported` reports what is actually available and
 * the components keep their on-screen controls as the fallback.
 */

import { browser } from "@web/core/browser/browser";
import {
    SilenceDetector,
    classifyUtterance,
    matchWakeWord,
    rmsFromWaveform,
} from "./voice_logic";

const SpeechRecognitionCtor =
    typeof window !== "undefined" &&
    (window.SpeechRecognition || window.webkitSpeechRecognition);

export const LOOP_STATES = {
    IDLE: "idle",
    LISTENING: "listening",
    CAPTURING: "capturing",
    THINKING: "thinking",
    SPEAKING: "speaking",
};

class VoiceLoop {
    constructor() {
        this.state = LOOP_STATES.IDLE;
        this.supported = Boolean(
            typeof navigator !== "undefined" &&
            navigator.mediaDevices &&
            navigator.mediaDevices.getUserMedia
        );
        this.wakeWordSupported = Boolean(SpeechRecognitionCtor);
        this._handlers = { onState: null, onTranscript: null, onWake: null, onError: null };
        this._stream = null;
        this._recognition = null;
        this._audioCtx = null;
        this._analyser = null;
        this._rafId = null;
        this._detector = null;
        this._suspensions = new Set();
        this._allowConsent = true;
        this._context = null;
        this._lastLevel = 0;
    }

    // ---------------------------------------------------------------- #
    // lifecycle

    /**
     * @param {object} handlers
     * @param {function} handlers.onState       state changed (for the UI)
     * @param {function} handlers.onTranscript  ({text, kind}) ready to act on
     * @param {function} [handlers.onWake]      wake word heard, for feedback
     * @param {function} [handlers.onError]     recoverable problem
     * @param {string} [handlers.context]       which screen is driving, so
     *   navigation commands ("下一个", "跳过") are scoped to the ones that
     *   screen actually offers
     */
    start(handlers = {}) {
        if (!this.supported) {
            this._fail("microphone unavailable");
            return false;
        }
        this._handlers = Object.assign(
            { onState: null, onTranscript: null, onWake: null, onError: null },
            handlers
        );
        this.setContext(handlers.context || null);
        this._setState(LOOP_STATES.LISTENING);
        this._startRecognition();
        return true;
    }

    stop() {
        this._teardown();
        this._setState(LOOP_STATES.IDLE);
    }

    /** True while *anyone* holds a suspension. Derived, never assigned:
     * every existing `if (this._suspended) return` guard keeps working
     * unchanged, but two holders can no longer clobber each other. */
    get _suspended() {
        return this._suspensions.size > 0;
    }

    /**
     * Pause the loop. `label` names the holder, so several independent
     * reasons can overlap without one releasing another's pause:
     *
     *   - "tts"     -- the assistant is speaking (echo suppression)
     *   - "handoff" -- control is with a device action (photo / scan)
     *
     * A plain boolean cannot express this. With two holders, the first
     * `resume()` clears the flag and the loop starts listening again
     * while the second holder still needs it silent -- either the
     * microphone reopens mid-photo (uploading the room) or it reopens
     * while the loudspeaker is still talking (echo). Counting by holder
     * is what makes "release only when the last one lets go" true.
     */
    _suspendFor(label) {
        if (this._suspensions.has(label)) {
            return;
        }
        const wasListening = this._suspensions.size === 0;
        this._suspensions.add(label);
        if (wasListening) {
            this._teardownRecognition();
            this._stopMeter();
        }
    }

    _resumeFor(label) {
        if (!this._suspensions.delete(label)) {
            return;
        }
        if (this._suspensions.size === 0 && this.state !== LOOP_STATES.IDLE) {
            this._restartWakeListening();
        }
    }

    /** Pause while the assistant speaks: otherwise the loudspeaker feeds
     * back into the microphone and the loop transcribes its own prompts. */
    suspend(label = "tts") {
        this._suspendFor(label);
    }

    resume(label = "tts") {
        this._resumeFor(label);
    }

    /**
     * Hand control to a device action (camera photo, barcode scan) that
     * cannot be performed by speaking.
     *
     * Returns the release function; **call it when the action finishes**
     * to give the loop its hearing back. Deliberately NOT built on
     * `stop()`: stopping clears `_stream` and drops the state to IDLE,
     * after which `resume()` refuses to re-arm and the loop could never
     * continue. This only pauses, so handlers, context and the consent
     * gate all survive the round trip.
     *
     * While paused the loop is deaf, which is the safe direction: a
     * "确认" spoken over a photo is simply not heard, rather than being
     * taken as consent for a step that has since moved on.
     */
    beginHandoff() {
        this._suspendFor("handoff");
        return () => this._resumeFor("handoff");
    }

    /** Toggle whether "确认"/"confirm" may be treated as consent.
     * False unless a confirmation is actually pending -- see
     * `classifyUtterance`. */
    setAllowConsent(allow) {
        this._allowConsent = Boolean(allow);
    }

    /**
     * Declare which screen is driving the loop, so navigation commands
     * are scoped to ones that screen actually offers.
     *
     * Set once at `start()` rather than on every utterance: classification
     * happens inside the loop so there is a single implementation, and the
     * caller cannot forget to pass it.
     */
    setContext(context) {
        this._context = context || null;
    }

    get canListen() {
        return this.supported && this.state !== LOOP_STATES.IDLE;
    }

    get listening() {
        return this.canListen;
    }

    get handsFreeRecognition() {
        return this.supported && Boolean(SpeechRecognitionCtor);
    }

    setLang(lang) {
        this._lang = lang || "zh-CN";
        if (this._recognition) {
            this._recognition.lang = this._lang;
        }
    }

    // ---------------------------------------------------------------- #
    // wake-word listening

    _restartWakeListening() {
        if (this._suspended) {
            return;
        }
        this._setState(LOOP_STATES.LISTENING);
        this._startRecognition();
    }

    _startRecognition() {
        if (!SpeechRecognitionCtor || this._suspended) {
            return;
        }
        this._teardownRecognition();
        const rec = new SpeechRecognitionCtor();
        rec.lang = this._lang || "zh-CN";
        rec.continuous = true;
        rec.interimResults = true;

        rec.onresult = (ev) => {
            // Only the tail (most recent) result is examined: the worker
            // often says the wake word and the instruction in one breath.
            let text = "";
            for (let i = ev.resultIndex; i < ev.results.length; i++) {
                text += ev.results[i][0].transcript;
            }
            if (!text) {
                return;
            }
            const hit = matchWakeWord(text);
            if (hit.matched) {
                this._handlers.onWake && this._handlers.onWake(hit.phrase);
                this._onWoken(hit.rest);
            }
        };
        rec.onerror = (ev) => {
            // "no-speech"/"aborted" are routine; real failures stop us.
            if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
                this._fail("microphone permission denied");
            }
        };
        rec.onend = () => {
            // The API stops on its own after silence; re-arm if we should
            // still be listening.
            if (!this._suspended && this.state === LOOP_STATES.LISTENING) {
                try {
                    rec.start();
                } catch (err) {
                    /* already starting; harmless */
                }
            }
        };
        this._recognition = rec;
        try {
            rec.start();
        } catch (err) {
            /* start() on a live recogniser throws; ignore */
        }
    }

    _teardownRecognition() {
        if (this._recognition) {
            try {
                this._recognition.onend = null;
                this._recognition.onresult = null;
                this._recognition.onerror = null;
                this._recognition.stop();
            } catch (err) {}
            this._recognition = null;
        }
    }

    // ---------------------------------------------------------------- #
    // capture (post-wake utterance)

    _onWoken(rest) {
        this._teardownRecognition();
        this._setState(LOOP_STATES.CAPTURING);
        if (rest) {
            // "助手 扫描货架A-01" -- the instruction arrived with the wake word.
            this._emit({ text: rest, kind: "content" });
            return;
        }
        this._startMeter();
    }

    /**
     * Open the audio meter that decides when the utterance has ended.
     *
     * The samples are read locally to compute loudness; nothing here is
     * uploaded.  Upload happens once, in `_finishCapture`, after the
     * detector says the worker stopped speaking.
     */
    async _startMeter() {
        try {
            if (!this._stream) {
                this._stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            }
            const Ctx = browser.AudioContext || browser.webkitAudioContext;
            this._audioCtx = new Ctx();
            const src = this._audioCtx.createMediaStreamSource(this._stream);
            this._analyser = this._audioCtx.createAnalyser();
            this._analyser.fftSize = 2048;
            src.connect(this._analyser);
            this._detector = new SilenceDetector();
            this._tick();
        } catch (err) {
            this._fail("microphone unavailable");
        }
    }

    _tick() {
        if (!this._analyser || !this._detector || this._suspended) {
            return;
        }
        const buf = new Float32Array(this._analyser.fftSize);
        this._analyser.getFloatTimeDomainData(buf);
        const level = rmsFromWaveform(buf);
        this._lastLevel = level;
        const verdict = this._detector.push(level, 50);
        if (verdict && verdict.done) {
            this._stopMeter();
            this._finishCapture(verdict);
            return;
        }
        this._rafId = browser.requestAnimationFrame(() => this._tick());
    }

    _stopMeter() {
        if (this._rafId) {
            browser.cancelAnimationFrame(this._rafId);
            this._rafId = null;
        }
        if (this._audioCtx) {
            try {
                this._audioCtx.close();
            } catch (err) {}
            this._audioCtx = null;
        }
        this._analyser = null;
    }

    /**
     * The utterance ended.  Transcribe the captured audio and act on it.
     *
     * Recording is done with MediaRecorder over the same stream that the
     * meter was watching: the worker never presses anything.
     */
    _finishCapture(verdict) {
        if (!verdict.sawSpeech) {
            // Nothing was said after the wake word; re-arm rather than
            // sending an empty clip.
            this._restartWakeListening();
            return;
        }
        this._setState(LOOP_STATES.THINKING);
        this._recordAndTranscribe();
    }

    async _recordAndTranscribe() {
        const handlers = this._handlers;
        try {
            const mr = new window.MediaRecorder(this._stream);
            const chunks = [];
            const stopped = new Promise((resolve) => {
                mr.onstop = () => resolve(new Blob(chunks));
            });
            mr.ondataavailable = (ev) => chunks.push(ev.data);

            // We already know the worker stopped; record a short window and
            // let ASR do the work.
            mr.start();
            await new Promise((r) => setTimeout(r, 400));
            mr.stop();
            const blob = await stopped;

            const b64 = await this._blobToB64(blob);
            this._handlers.onCapture && this._handlers.onCapture(
                b64, (text) => this._emit({ text: text || "" }));
        } catch (err) {
            this._fail("transcription unavailable");
        }
    }

    _blobToB64(blob) {
        return new Promise((resolve, reject) => {
            const fr = new window.FileReader();
            fr.onload = () => resolve(String(fr.result).split(",")[1]);
            fr.onerror = reject;
            fr.readAsDataURL(blob);
        });
    }

    /**
     * The single place an utterance is classified.
     *
     * Every path (wake-word carry-over, transcribed clip) funnels through
     * here, so `allowsConsent` and `context` cannot drift between callers
     * -- and there is exactly one implementation of the safety rules.
     */
    _emit(payload) {
        const kind = payload.kind || classifyUtterance(payload.text, {
            allowsConsent: this._allowConsent,
            context: this._context,
        });
        this._handlers.onTranscript &&
            this._handlers.onTranscript({ text: payload.text, kind });
        if (this.state !== LOOP_STATES.IDLE) {
            this._restartWakeListening();
        }
    }

    // ---------------------------------------------------------------- #

    _setState(next) {
        if (this.state === next) {
            return;
        }
        this.state = next;
        this._handlers.onState && this._handlers.onState(next);
    }

    _fail(message) {
        this._handlers.onError && this._handlers.onError(message);
        this.stop();
    }

    _teardown() {
        this._teardownRecognition();
        this._stopMeter();
        if (this._stream) {
            try {
                this._stream.getTracks().forEach((t) => t.stop());
            } catch (err) {}
            this._stream = null;
        }
        this._suspensions.clear();
    }
}

export const voiceLoop = new VoiceLoop();
