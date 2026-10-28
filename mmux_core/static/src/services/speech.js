/**
 * Copyright (C) 2026 jeffery9@gmail.com & geninit
 * License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
 */
/** @odoo-module **/

import { tts } from "./tts";
import { voiceLoop } from "./voice_loop";

/**
 * Speak a prompt without the microphone hearing it.
 *
 * Every hands-free surface needs the same three steps in the same
 * order, and getting the order wrong is audible rather than visible:
 *
 *   1. take a *handoff* suspension, so the loop stops listening;
 *   2. say the words;
 *   3. release on `onEnd`, so the loop listens again only once the
 *      loudspeaker has actually gone quiet.
 *
 * Suspending after speaking (or resuming before it finished) lets the
 * loudspeaker feed back into the microphone and the loop transcribes
 * its own prompt. That is why this lives in one place instead of being
 * repeated in four components with four chances to drift.
 *
 * The handoff label is deliberate: it is a *different* holder from the
 * "tts" label used elsewhere, so a photo round trip in progress is not
 * cut short by a prompt finishing (see `voiceLoop._suspendFor`).
 *
 * @param {string} text            what to say
 * @param {object} [opts]
 * @param {boolean} [opts.enabled] false to stay silent (e.g. not hands-free)
 * @param {function} [opts.onEnd]  called after the words finish
 */
export function speakGuarded(text, { enabled = true, onEnd } = {}) {
    if (!text || !enabled) {
        onEnd && onEnd();
        return;
    }
    tts.stop();
    const release = voiceLoop.beginHandoff();
    tts.speak(text, {
        onEnd: () => {
            release();
            onEnd && onEnd();
        },
    });
}
