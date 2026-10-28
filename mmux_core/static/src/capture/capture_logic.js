/**
 * Copyright (C) 2026 jeffery9@gmail.com & geninit
 * License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
 */
/** @odoo-module **/

/**
 * Device capture: the decisions behind photo and barcode input (PRD §2.6).
 *
 * Photographing and scanning are the two things a worker genuinely cannot
 * do by speaking -- there is no way to talk a picture into existence. So
 * the product question is not "should they touch the screen" but "how do
 * we hand control to the device and take it back without losing the
 * worker's place".
 *
 * Everything here is pure: no DOM, no OWL, no camera, no timers. The
 * parts that are easy to get subtly wrong -- how far to shrink an image,
 * how aggressive barcode de-duplication should be, how to read a vision
 * reply whose shape varies -- are separated out so they can be tested
 * without a browser, the same way `voice_logic.js` does it.
 */

/** Longest edge of a stored photo, in pixels.
 * Sized so a nameplate or a damaged part is still legible while a single
 * frame stays around 100-250 KB -- field networks are the constraint. */
export const MAX_EDGE = 1280;

/** JPEG quality for stored photos. Visually indistinguishable from 0.9
 * at this size, roughly half the bytes. */
export const JPEG_QUALITY = 0.7;

/** Barcodes re-decoded within this window are the same physical scan. */
export const BARCODE_DEDUPE_MS = 1200;

/**
 * Scale a frame down so its longest edge fits `maxEdge`, keeping the
 * aspect ratio. Never scales up: a small frame is left alone rather
 * than interpolated into a blurrier, larger one.
 *
 * Returns integer dimensions, because canvas dimensions must be integers.
 */
export function fitWithin(width, height, maxEdge = MAX_EDGE) {
    const w = Number(width) || 0;
    const h = Number(height) || 0;
    if (w <= 0 || h <= 0 || maxEdge <= 0) {
        return { width: 0, height: 0 };
    }
    const longest = Math.max(w, h);
    if (longest <= maxEdge) {
        return { width: Math.round(w), height: Math.round(h) };
    }
    const scale = maxEdge / longest;
    // `max(1, ...)` so a very long thin frame cannot round its short edge
    // to zero and produce a canvas that throws.
    return {
        width: Math.max(1, Math.round(w * scale)),
        height: Math.max(1, Math.round(h * scale)),
    };
}

/** Camera constraints: rear lens preferred, 720p, no audio track.
 * Requesting audio:false matters -- an audio track here would be a second
 * microphone stream competing with the voice loop's own. */
export function captureConstraints(facing = "environment") {
    return {
        video: {
            facingMode: { ideal: facing },
            width: { ideal: 1280 },
            height: { ideal: 720 },
        },
        audio: false,
    };
}

/** Canvas pixels as bare base64, with the `data:` header stripped
 * (Odoo's `ir.attachment.datas` and the `/mmux/*` routes both expect it
 * without the prefix). */
export function b64FromCanvas(canvas, type = "image/jpeg",
                              quality = JPEG_QUALITY) {
    const url = canvas.toDataURL(type, quality);
    return String(url).split(",")[1] || "";
}

/**
 * Reports whether a decoded barcode is a *new* reading.
 *
 * A camera decodes the same code on every frame it stays in view, so
 * without this one barcode would be submitted dozens of times. The rule
 * is deliberately narrow: only an *identical* value inside the window is
 * suppressed, so scanning two different items in quick succession still
 * works.
 */
export class BarcodeDeduper {
    constructor({ windowMs = BARCODE_DEDUPE_MS } = {}) {
        this.windowMs = windowMs;
        this.last = null;
        this.at = 0;
    }

    /** @returns {boolean} true if this reading should be acted on. */
    accept(code, now = Date.now()) {
        const value = String(code == null ? "" : code).trim();
        if (!value) {
            return false;
        }
        if (value === this.last && now - this.at < this.windowMs) {
            // Same code still in view: slide the window so a long stare
            // does not re-fire once the original window lapses.
            this.at = now;
            return false;
        }
        this.last = value;
        this.at = now;
        return true;
    }

    reset() {
        this.last = null;
        this.at = 0;
    }
}

/**
 * Pull the readable text out of a `/mmux/vision` reply.
 *
 * `analyze_image` is not a single shape (see `mmux_providers.py` and
 * `mmux_service_loop.py`), and unlike `transcribe` it carries **no
 * `fallback` key** -- copying the transcription handling here would
 * silently read `undefined`:
 *
 *   { data: {...}, raw: "..." }          provider parsed the JSON
 *   { data: null, raw: "...", text: "..."} provider could not parse it
 *   { error: "..." }                     vision unavailable
 *
 * So probe in order and return `null` when there is genuinely nothing,
 * letting the caller say so rather than filling a field with "undefined".
 */
export function pickVisionText(payload) {
    if (!payload || payload.error) {
        return null;
    }
    if (typeof payload.text === "string" && payload.text.trim()) {
        return payload.text.trim();
    }
    const data = payload.data;
    if (typeof data === "string" && data.trim()) {
        return data.trim();
    }
    if (data && typeof data === "object") {
        for (const key of ["text", "normalized_text", "value", "barcode"]) {
            const v = data[key];
            if (typeof v === "string" && v.trim()) {
                return v.trim();
            }
        }
    }
    if (typeof payload.raw === "string" && payload.raw.trim()) {
        const raw = payload.raw.trim();
        try {
            const parsed = JSON.parse(raw);
            if (parsed && typeof parsed === "object") {
                for (const key of ["text", "normalized_text", "value", "barcode"]) {
                    const v = parsed[key];
                    if (typeof v === "string" && v.trim()) {
                        return v.trim();
                    }
                }
            }
        } catch (err) {
            // Not JSON -- the model answered in prose, which is still a
            // usable reading.
        }
        return raw;
    }
    return null;
}

/**
 * First number in a piece of text, as an integer, or `null`.
 *
 * Used to fill a quantity step from an OCR result. Returning `null` when
 * there is no number is deliberate: the caller must not substitute 0 or
 * guess, because the value ends up in a stock move.
 */
export function pickQuantity(text) {
    const m = String(text == null ? "" : text).match(/-?\d+(?:\.\d+)?/);
    if (!m) {
        return null;
    }
    const n = Number(m[0]);
    return Number.isFinite(n) ? n : null;
}
