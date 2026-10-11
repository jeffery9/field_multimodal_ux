/** @odoo-module **/

/**
 * Text-to-speech helper using the browser's Web Speech API — zero
 * external service, works offline (PRD §2.6 progressive enhancement:
 * if speechSynthesis is unavailable the guide falls back to on-screen
 * prompts only).
 *
 * ## Why the generation counter
 *
 * `speak()` starts with `speechSynthesis.cancel()`, which synchronously
 * fires the PREVIOUS utterance's `onend`. Callers pass
 * `{ onEnd: () => voiceLoop.resume() }`, so without a guard the sequence
 *
 *      speak(A, onEnd=resume)   ->  A playing
 *      speak(B, onEnd=resume)   ->  cancel() fires A.onEnd -> resume()
 *
 * resumes the microphone **while B is still being spoken**, and the
 * loudspeaker feeds straight back into it. That is the exact echo the
 * suspend/resume pair exists to prevent, so `cancel()` was undoing it.
 *
 * The fix is a generation tag: `finish` only runs if the utterance it
 * belongs to is still the current one. A `cancel()`-triggered stale
 * `onend` therefore finds itself superseded and is swallowed.
 */
export const tts = {
    enabled: typeof window !== "undefined" && "speechSynthesis" in window,
    _lang: "zh-CN",
    /** Incremented per utterance; identifies the current generation. */
    _seq: 0,
    /** `{ tag, finish }` of the utterance allowed to report completion. */
    _current: null,

    setLang(lang) {
        this._lang = lang || "zh-CN";
    },

    /** The tag the caller should hand back to `suspend`/`resume`, so a
     * superseded utterance cannot release a suspension it no longer owns. */
    speak(text, { onEnd } = {}) {
        const tag = "tts:" + (++this._seq);
        if (!this.enabled || !text) {
            onEnd && onEnd(tag);
            return tag;
        }
        // Fires at most once, and only while this utterance is the
        // current one -- see the header comment.
        const finish = () => {
            if (!this._current || this._current.tag !== tag) {
                return;
            }
            this._current = null;
            onEnd && onEnd(tag);
        };
        try {
            // May fire the previous utterance's onend; `finish` above
            // suppresses it because `_current` has not been replaced yet
            // ... which is why `_current` is set BEFORE speaking.
            this._current = { tag, finish };
            window.speechSynthesis.cancel();
            const utter = new SpeechSynthesisUtterance(text);
            utter.lang = this._lang;
            utter.rate = 1.05;
            utter.onend = finish;
            utter.onerror = finish;
            window.speechSynthesis.speak(utter);
        } catch (err) {
            finish();
        }
        return tag;
    },

    stop() {
        if (this.enabled) {
            try {
                window.speechSynthesis.cancel();
            } catch (err) {}
        }
    },
};
