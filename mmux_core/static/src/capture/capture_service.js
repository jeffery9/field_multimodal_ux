/** @odoo-module **/

import { mmuxApi } from "../services/mmux_api";
import {
    pickQuantity,
    pickVisionText,
} from "./capture_logic";
import {
    captureIntentFor,
    shouldRecognize,
    stepFieldFor,
    valueForField,
} from "./capture_intent";

/**
 * The one place a captured photo becomes data.
 *
 * Every interface that can open the camera ends up needing the same two
 * follow-up actions, and each of them has a way to go wrong:
 *
 *   - **OCR** must go through `/mmux/vision`, which returns three
 *     different shapes and -- unlike `/mmux/transcribe` -- has no
 *     `fallback` key. Reading it like a transcription reply yields
 *     `undefined`, which then gets typed into the worker's draft box.
 *   - **Evidence** must be an `ir.attachment` reachable from the record,
 *     not a base64 string parked in a JS variable where it is lost on
 *     the next page change.
 *
 * Neither answer needs a transaction: this module resolves *what the
 * data is*, and the caller decides whether to submit. In particular it
 * never calls `execute` -- a mis-read photo must not trigger a business
 * write. The photo travels to the record later, through the same
 * `/mmux/execute` funnel as any other parameter, so the ExecutionGuard's
 * ACL, record-rule and audit steps still apply.
 */

/** Read text out of a photo. Never throws: an unavailable vision
 * provider degrades to `null` so the caller can say so. */
export async function recognizePhoto(imageB64, purpose = "ocr") {
    try {
        const res = await mmuxApi.vision(imageB64, purpose);
        return pickVisionText(res);
    } catch (err) {
        return null;
    }
}

/**
 * Recognize a photo and report what it should fill.
 *
 * @returns {{text: string|null, quantity: number|null}}
 */
export async function recognizeForStep(imageB64, inputType) {
    if (!shouldRecognize(inputType)) {
        return { text: null, quantity: null };
    }
    const text = await recognizePhoto(imageB64, "ocr");
    return { text, quantity: text == null ? null : pickQuantity(text) };
}

/**
 * The photos parameter for a capability call.
 *
 * `ir.attachment` expects bare base64, but a photo that came from
 * `FileReader` or a well-meaning caller may still carry the `data:`
 * header. Stripping it here means no caller has to remember (the server
 * strips it too -- defense in depth, since this one is client-side).
 */
export function photosParam(images) {
    const list = Array.isArray(images) ? images : [images];
    const out = [];
    for (const item of list) {
        if (!item) continue;
        const b64 = typeof item === "string" ? item : item.b64;
        if (typeof b64 !== "string" || !b64) continue;
        out.push({ image_b64: b64.split(",").pop() });
    }
    return out;
}

/**
 * Describe what the worker should hear after a capture, given the step.
 *
 * Kept here rather than in each component so the same situation is
 * described the same way on every screen -- a worker who learns "已拍下，
 * 正在识别" in the guide should not hear something else in the panel.
 */
export function captureNotice(field, value) {
    if (value == null) {
        return { ok: false, text: "没能识别出内容，请重拍或直接输入" };
    }
    if (field === "quantity") {
        return { ok: true, text: `识别到数量 ${value}，请确认后提交` };
    }
    if (field === "barcode") {
        return { ok: true, text: `识别到条码 ${value}，请确认后提交` };
    }
    return { ok: true, text: `已识别：${value}，请确认后提交` };
}

export { captureIntentFor, shouldRecognize, stepFieldFor, valueForField };
