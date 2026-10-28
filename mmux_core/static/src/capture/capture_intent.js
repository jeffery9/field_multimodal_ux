/**
 * Copyright (C) 2026 jeffery9@gmail.com & geninit
 * License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
 */
/** @odoo-module **/

/**
 * Which steps want which device, and what to do with the result.
 *
 * Five interfaces can now open a camera, and they all face the same
 * three questions:
 *
 *   1. Does this step want a photo, a barcode, or neither?
 *   2. Does the decoded barcode belong in the record field, or in the
 *      text box the worker types into?
 *   3. Does a photo from this step go to OCR, or is it evidence for the
 *      business record being written?
 *
 * Answering those five times would have produced five subtly different
 * policies -- exactly how a "扫码" button ends up auto-submitting in one
 * screen and filling a draft in another. So the answers live here, as
 * data, and the components only render them.
 *
 * Everything is pure: a table, two lookups. No DOM, no OWL.
 */

/** Step `input_type` values that a camera can satisfy, and how. */
const STEP_CAPTURE = {
    // The step's whole purpose is recording evidence, so the photo IS the
    // deliverable -- it belongs in the record's attachments.
    evidence: { purpose: "evidence", field: "photos" },
    photo: { purpose: "evidence", field: "photos" },
    // The step wants a value the camera can read out. OCR fills the step's
    // own text field; the worker still confirms.
    ocr: { purpose: "ocr", field: "text" },
    receipt: { purpose: "ocr", field: "text" },
    // Scanning is the point: the decoded code is the answer itself.
    scan: { purpose: "scan", field: "barcode" },
    // A quantity step is a plausible place to photograph a nameplate --
    // but only to READ a number, never to submit one.
    voice_qty: { purpose: "ocr", field: "quantity" },
    qty: { purpose: "ocr", field: "quantity" },
    // `confirm` and `capability` are decision steps. A camera there would
    // be a way to manufacture a decision, so it is not offered.
};

/**
 * Describe the camera a guided step should offer.
 *
 * @param {string} inputType the server's own step `input_type`
 * @returns {{purpose: string, field: string}|null} null = no camera
 */
export function captureIntentFor(inputType) {
    const key = String(inputType == null ? "" : inputType).trim();
    return STEP_CAPTURE[key] || null;
}

/**
 * Where a free-text result lands in a guided step.
 *
 * The mapping is deliberately narrow: a value only ever fills the field
 * the step ALREADY has. An OCR result for a `confirm` step has nowhere
 * to go, so it is refused rather than smuggled into `_advance`, because
 * `_advance` is what writes to the business record.
 *
 * @returns {"text"|"barcode"|"quantity"|null} null = the step cannot
 *   accept a captured value, so the caller must not submit anything.
 */
export function stepFieldFor(inputType) {
    const intent = captureIntentFor(inputType);
    return intent ? intent.field : null;
}

/**
 * Turn raw OCR text into the value a step's field should hold.
 *
 * Returns `null` -- never 0, never the raw text -- when the text has no
 * usable value for that field, so the caller asks again instead of
 * putting a wrong number into a stock move.
 *
 * @param {string|null} text      OCR output, already unwrapped
 * @param {number|null} quantity  parsed number, or null
 * @returns {string|number|null}
 */
export function valueForField(field, text, quantity) {
    if (text == null || !String(text).trim()) {
        return null;
    }
    const clean = String(text).trim();
    if (field === "quantity") {
        return quantity == null ? null : quantity;
    }
    if (field === "barcode") {
        // Barcodes carry no meaningful whitespace; OCR inserts plenty.
        const code = clean.replace(/[\s\-_,.]/g, "");
        return code || null;
    }
    return clean;
}

/**
 * Should a captured photo also be OCR'd before being filed?
 *
 * Yes for a step whose point is reading a value (`ocr`, `receipt`,
 * `voice_qty`): the OCR result is what makes the photo useful. No for an
 * evidence step -- there the photo is the record, and running a
 * recognition pass over it would invite the worker to type a value out
 * of it, which is a different (and unconfirmed) business write.
 */
export function shouldRecognize(inputType) {
    const intent = captureIntentFor(inputType);
    return intent ? intent.purpose === "ocr" : false;
}
