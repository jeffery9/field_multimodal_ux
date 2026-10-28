/**
 * Copyright (C) 2026 jeffery9@gmail.com & geninit
 * License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
 */
/** @odoo-module **/

/**
 * Hands-free voice loop (PRD §2.6, FR-001).
 *
 * The goal this serves: a field worker with both hands occupied (carrying
 * a box, wearing gloves, phone in a pocket) must be able to finish a task
 * WITHOUT touching the screen.  That requires four things, and this module
 * exists because none of them can be bolted on independently:
 *
 *   1. a wake word, so the microphone can stay open without streaming
 *      everything the worker says to a provider;
 *   2. automatic end-of-speech detection, so the worker never has to press
 *      "stop" (the previous design required two taps per sentence);
 *   3. voice confirmation for high-risk steps, so the L2 gate that
 *      currently forces a button tap also becomes hands-free;
 *   4. suppression of capture while the assistant is speaking, otherwise
 *      the loudspeaker feeds back into the microphone and the loop
 *      transcribes its own prompts.
 *
 * ## Privacy: what actually leaves the device
 *
 * Audio before the wake word is compared **in memory** against the wake
 * phrases and then dropped.  It is never uploaded, never written to disk
 * and never retained.  Only audio captured *after* a wake word is sent for
 * transcription.  This is the whole reason the wake word exists rather
 * than simply leaving the recorder running.
 *
 * ## Why the matching logic is separated out
 *
 * `matchWakeWord`, `classifyUtterance` and `SilenceDetector` are pure (no
 * DOM, no timers, no audio APIs).  They hold the decisions that are easy
 * to get subtly wrong -- a missed wake word is a broken product, and a
 * false confirmation executes a real business action -- so they are
 * written to be unit-testable without a browser.
 */

/** Phrases that arm the loop. Lowercased, punctuation stripped. */
export const WAKE_WORDS = [
    "你好助手",
    "嗨助手",
    "助手",
    "hey assistant",
    "hi assistant",
    "assistant",
];

/** Affirmatives accepted as consent for a high-risk (L2) step. */
export const CONFIRM_WORDS = ["确认", "确定", "执行", "confirm", "yes", "ok"];

/** Negatives: abandon the pending action. */
export const CANCEL_WORDS = ["取消", "放弃", "不", "cancel", "no", "stop"];

/**
 * Navigation / control commands, grouped by the screen they apply to.
 *
 * Full hands-free means more than dictating content: the worker also has
 * to move the workflow along ("next task", "I need help", "skip this
 * step", "say that again") without reaching for the screen.
 *
 * The grouping is what keeps this honest. A phrase is only a command in a
 * context that actually HAS that command, so "skip" cannot silently
 * discard a step in a screen that has no step to skip. Commands are
 * whole-utterance matches for the same reason consent is: "下一步是
 * 什么" is a question, not an instruction to advance.
 */
export const COMMANDS = {
    // move to the next task in the task loop
    next: {
        contexts: ["taskloop"],
        words: ["下一个", "下一个任务", "继续", "next", "next task"],
    },
    // ask for guidance on the current task
    help: {
        contexts: ["taskloop", "guide", "agent"],
        words: ["怎么办", "帮助", "怎么弄", "怎么做", "help", "how"],
    },
    // move past the current step without doing it
    skip: {
        contexts: ["guide", "agent"],
        words: ["跳过", "略过", "skip"],
    },
    // repeat the last spoken prompt (the worker missed it)
    repeat: {
        contexts: ["panel", "guide", "taskloop", "agent"],
        words: ["再说一次", "重复", "再念一遍", "repeat", "again", "say again"],
    },
};

/** The context codes a caller may pass. Kept closed so a typo fails loud. */
export const CONTEXTS = ["panel", "guide", "taskloop", "agent"];

/**
 * Normalise a transcript for matching.
 *
 * ASR output is inconsistent about punctuation, spacing and casing
 * ("Hey, Assistant." vs "hey assistant").  Matching on the raw string
 * would make the wake word unreliable, so everything is folded to a
 * comparable form first.  CJK is not case-folded (it has no case) and
 * interior spaces are preserved so "no" cannot be carved out of
 * "notebook" -- word boundaries are re-checked by the caller.
 */
export function normalizePhrase(text) {
    if (!text) {
        return "";
    }
    return String(text)
        .toLowerCase()
        .replace(/[.,!?;:、，。！？；：""''"'"'（）()\[\]]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
}

/**
 * Does the transcript contain the wake word?
 *
 * Substring matching, deliberately: the wake word is normally preceded by
 * an interjection ("那个…助手") or followed by the instruction in the same
 * breath ("助手，扫描这个").  Requiring an exact utterance would force
 * unnatural speech.
 *
 * Latin phrases are matched on word boundaries so "assistant" does not
 * fire inside a longer word; CJK has no word boundaries, so those match by
 * substring.
 *
 * @returns {{matched: boolean, phrase: string, rest: string}}
 *   `rest` is what followed the wake word, so a single-breath
 *   "助手 扫描货架" can be treated as already containing an instruction.
 */
export function matchWakeWord(transcript) {
    const norm = normalizePhrase(transcript);
    if (!norm) {
        return { matched: false, phrase: "", rest: "" };
    }
    for (const raw of WAKE_WORDS) {
        const phrase = normalizePhrase(raw);
        let at = -1;
        if (/[\u4e00-\u9fff]/.test(phrase)) {
            at = norm.indexOf(phrase);
        } else {
            // word-boundary match for space-delimited scripts
            const re = new RegExp(`(?:^|\\s)${phrase.replace(
                /[.*+?^${}()|[\]\\]/g, "\\$&")}(?:$|\\s)`);
            const m = re.exec(norm);
            at = m ? m.index + (m[0].startsWith(" ") ? 1 : 0) : -1;
        }
        if (at >= 0) {
            return {
                matched: true,
                phrase: raw,
                rest: norm.slice(at + phrase.length).trim(),
            };
        }
    }
    return { matched: false, phrase: "", rest: "" };
}

/**
 * Classify a captured utterance.
 *
 * Returns one of:
 *   "confirm" | "cancel" | "next" | "help" | "skip" | "repeat"
 *   | "content" | "empty"
 *
 * ## Safety-critical ordering
 *
 * Consent is decided FIRST and unconditionally, before any command
 * lookup, because the two must never be confused: "确认" while a delivery
 * validation is pending is consent, and it must not be shadowed by some
 * navigation rule. Only if the words are not consent/refusal are the
 * navigation commands considered, and only those whose `contexts` include
 * the caller's context.
 *
 * ## Why whole-utterance matching, for everything
 *
 * A fragment must never trigger a control action. Requiring the whole
 * utterance to equal the phrase is what makes these safe:
 *
 *   - "别确认" ("don't confirm") contains 确认 and must NOT approve;
 *   - "下一步是什么" ("what's the next step?") contains 下一个 and must
 *     NOT advance -- it is a question;
 *   - "ok let me think" must NOT be treated as consent.
 *
 * Anything unmatched is ordinary task content for the intent parser.
 *
 * @param {string} transcript
 * @param {{allowsConsent?: boolean, context?: string}} opts
 *   `allowsConsent` false means no confirmation is pending, so affirmative
 *   words are not consent at all. `context` scopes the navigation
 *   commands; an unknown context yields no commands rather than all of
 *   them, so a typo cannot widen what a word is allowed to do.
 * @returns {string}
 */
export function classifyUtterance(transcript, opts = {}) {
    const norm = normalizePhrase(transcript);
    if (!norm) {
        return "empty";
    }
    if (opts.allowsConsent !== false) {
        if (CONFIRM_WORDS.some((w) => norm === normalizePhrase(w))) {
            return "confirm";
        }
        if (CANCEL_WORDS.some((w) => norm === normalizePhrase(w))) {
            return "cancel";
        }
    }
    const context = opts.context;
    if (context && CONTEXTS.includes(context)) {
        for (const [name, spec] of Object.entries(COMMANDS)) {
            if (!spec.contexts.includes(context)) {
                continue;
            }
            if (spec.words.some((w) => norm === normalizePhrase(w))) {
                return name;
            }
        }
    }
    return "content";
}

/**
 * Detects the end of an utterance from a stream of loudness (RMS) samples.
 *
 * The old design needed a second tap to stop recording, which is exactly
 * the "hands on the screen" the feature exists to remove.  Instead the
 * loop watches how loud the input is: once speech has started, a
 * sufficiently long quiet stretch means the worker has finished the
 * sentence.
 *
 * Two properties matter for correctness:
 *
 *   - speech must be seen before silence counts, otherwise the loop would
 *     "finish" an utterance immediately while the worker is still drawing
 *     breath;
 *   - a short pause mid-sentence must NOT end it, so a worker who says
 *     "数量... 十二" is not cut off after the ellipsis.  That is what the
 *     hold duration controls.
 */
export class SilenceDetector {
    /**
     * @param {object} [opts]
     * @param {number} [opts.threshold] RMS below which input is "quiet"
     * @param {number} [opts.holdMs] quiet duration that ends the utterance
     * @param {number} [opts.maxMs] hard cap so a noisy room cannot record forever
     */
    constructor(opts = {}) {
        this.threshold = opts.threshold != null ? opts.threshold : 0.02;
        this.holdMs = opts.holdMs != null ? opts.holdMs : 800;
        this.maxMs = opts.maxMs != null ? opts.maxMs : 15000;
        this.reset();
    }

    reset() {
        this.sawSpeech = false;
        this.quietSince = null;
        this.elapsedMs = 0;
        this.done = false;
    }

    /** Feed one sample. `level` is an RMS in [0, 1]; `dtMs` is the step. */
    push(level, dtMs) {
        if (this.done) {
            return this.result();
        }
        this.elapsedMs += dtMs;
        const loud = level >= this.threshold;
        if (loud) {
            this.sawSpeech = true;
            this.quietSince = null;
        } else if (this.sawSpeech && this.quietSince === null) {
            this.quietSince = this.elapsedMs;
        }

        const quietFor = this.quietSince === null ? 0 : this.elapsedMs - this.quietSince;
        if (this.sawSpeech && quietFor >= this.holdMs) {
            this.done = true;
            return this.result("silence");
        }
        if (this.elapsedMs >= this.maxMs) {
            this.done = true;
            return this.result(this.sawSpeech ? "timeout" : "empty");
        }
        return null;
    }

    result(reason = "") {
        return {
            done: true,
            reason,
            durationMs: this.elapsedMs,
            sawSpeech: this.sawSpeech,
        };
    }
}

/**
 * Root-mean-square level of a time-domain waveform, in [0, 1].
 *
 * Exposed separately from the detector so the detector's decisions can be
 * tested with plain numbers rather than synthesised audio buffers.
 */
export function rmsFromWaveform(samples) {
    if (!samples || !samples.length) {
        return 0;
    }
    let sum = 0;
    for (let i = 0; i < samples.length; i++) {
        // AnalyserNode yields either [-1,1] floats or 0..255 bytes
        const v = samples[i] > 1 || samples[i] < -1 ? samples[i] / 128 - 1 : samples[i];
        sum += v * v;
    }
    return Math.sqrt(sum / samples.length);
}
