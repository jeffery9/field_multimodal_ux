/** @odoo-module **/

import { Component, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { speakGuarded } from "../services/speech";
import { voiceLoop } from "../services/voice_loop";
import {
    BarcodeDeduper,
    captureConstraints,
    fitWithin,
    b64FromCanvas,
} from "./capture_logic";

/** `window.BarcodeDetector` where the platform provides it. */
const BarcodeDetectorCtor =
    typeof window !== "undefined" && window.BarcodeDetector;

/**
 * Camera capture for the two things that cannot be spoken: a photo and
 * a barcode (PRD §2.6).
 *
 * ## Handing control over and taking it back
 *
 * A device action breaks the hands-free loop by definition -- there is no
 * way to talk a picture into existence. So this component *takes a
 * handoff suspension* on mount, which makes the voice loop stop
 * listening, and releases it on close, which gives the loop its hearing
 * back with its context and consent gate intact. The worker taps once to
 * take the photo and the conversation resumes where it left off.
 *
 * While the suspension is held the loop is deaf. That is the safe
 * direction: a "确认" spoken over a camera preview is simply not heard,
 * rather than being taken as consent for a step the work has moved past.
 *
 * ## Three ways in, because one is never enough
 *
 *   1. `getUserMedia` + `BarcodeDetector` -- live decode where supported.
 *   2. `getUserMedia` + shutter        -- photo, and scan-by-eye where
 *      the decoder is missing (Safari and Firefox have no
 *      `BarcodeDetector` at all).
 *   3. `<input type="file" capture>`   -- the system camera. On iOS this
 *      is the *only* path that works in a non-HTTPS or embedded WebView
 *      context, and on a desktop with no camera it is the only way to
 *      attach a photo someone else took.
 *
 * All three funnel into the same two callbacks, so a caller never has to
 * know which one produced the result.
 */
export class MmuxCapture extends Component {
    static template = "mmux_core.Capture";
    static props = {
        /** "photo" takes a still; "scan" also runs the barcode decoder. */
        purpose: { type: String, optional: true },
        /** Called with `(b64, meta)` once a still exists. */
        onCaptured: { type: Function, optional: true },
        /** Called with `(code)` on a decoded barcode. */
        onBarcode: { type: Function, optional: true },
        /** Called when the worker dismisses the camera. */
        onClose: { type: Function, optional: true },
    };
    static defaultProps = {
        purpose: "photo",
        onCaptured: null,
        onBarcode: null,
        onClose: null,
    };

    setup() {
        this.videoRef = useRef("video");
        this.canvasRef = useRef("canvas");
        this.fileRef = useRef("file");
        this._stream = null;
        this._rafId = null;
        this._detector = null;
        this._deduper = new BarcodeDeduper();
        this._releaseHandoff = null;
        this._alive = true;

        this.state = useState({
            ready: false,
            error: "",
            notice: "",
            canDecode: Boolean(BarcodeDetectorCtor),
            lastBarcode: "",
        });

        // Take the voice loop's hearing for the duration of the capture.
        // A suspension (not a stop) is what lets the conversation resume
        // afterwards with its context and consent gate intact.
        this._releaseHandoff = voiceLoop.beginHandoff();

        this._start().then(() => undefined);

        onWillUnmount(() => {
            this._alive = false;
            this._stopDecoding();
            this._releaseStream();
            this.close();
        });
    }

    // ---------------------------------------------------------------- //
    // stream lifecycle

    async _start() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            this._degrade(_t("此设备无法调用相机，请改用相册或手输。"));
            return;
        }
        try {
            this._stream = await navigator.mediaDevices.getUserMedia(
                captureConstraints()
            );
            if (!this._alive) {
                // Unmounted while the permission prompt was open.
                this._releaseStream();
                return;
            }
            const video = this.videoRef.el;
            if (video) {
                video.srcObject = this._stream;
                await video.play();
            }
            this.state.ready = true;
            if (this.props.purpose === "scan" && BarcodeDetectorCtor) {
                this._startDecoding();
            } else if (this.props.purpose === "scan") {
                this.state.notice = _t(
                    "此浏览器不能自动识别条码，请对准后拍照，或直接输入。"
                );
            }
        } catch (err) {
            this._degrade(_t("无法访问相机，请改用相册或手输。"));
        }
    }

    /** Permission denied or no camera: keep a usable path, say so. */
    _degrade(message) {
        this.state.error = message;
        this.state.ready = false;
        this._releaseStream();
    }

    /**
     * Release the camera. Called from unmount and from close, and
     * deliberately not optional: a live video track keeps the hardware
     * indicator lit, which reads to the worker as "it is filming me".
     */
    _releaseStream() {
        if (this._stream) {
            try {
                this._stream.getTracks().forEach((t) => t.stop());
            } catch (err) {}
            this._stream = null;
        }
        this.state.ready = false;
    }

    // ---------------------------------------------------------------- //
    // barcode decoding

    _startDecoding() {
        if (!BarcodeDetectorCtor) {
            return;
        }
        try {
            this._detector = new BarcodeDetectorCtor();
        } catch (err) {
            this._detector = null;
            return;
        }
        const step = async () => {
            if (!this._alive || !this._detector || !this.state.ready) {
                return;
            }
            try {
                const found = await this._detector.detect(this.videoRef.el);
                for (const hit of found || []) {
                    if (this._onDecoded(hit.rawValue)) {
                        return; // one scan is one result
                    }
                }
            } catch (err) {
                // Early Android builds throw on some frames; skip it.
            }
            this._rafId = window.requestAnimationFrame(step);
        };
        this._rafId = window.requestAnimationFrame(step);
    }

    _stopDecoding() {
        if (this._rafId) {
            window.cancelAnimationFrame(this._rafId);
            this._rafId = null;
        }
    }

    _onDecoded(raw) {
        const code = String(raw == null ? "" : raw).trim();
        // A camera re-reads the same code on every frame it stays in view.
        if (!this._deduper.accept(code)) {
            return false;
        }
        this.state.lastBarcode = code;
        this._stopDecoding();
        this.props.onBarcode && this.props.onBarcode(code);
        return true;
    }

    // ---------------------------------------------------------------- //
    // still capture

    /** Shutter: copy the current frame into a downscaled JPEG. */
    snapshot() {
        const video = this.videoRef.el;
        const canvas = this.canvasRef.el;
        if (!video || !canvas || !video.videoWidth) {
            this.state.error = _t("相机尚未就绪，请稍候再试。");
            return;
        }
        const { width, height } = fitWithin(video.videoWidth, video.videoHeight);
        canvas.width = width;
        canvas.height = height;
        canvas.getContext("2d").drawImage(video, 0, 0, width, height);
        const b64 = b64FromCanvas(canvas);
        this.props.onCaptured &&
            this.props.onCaptured(b64, {
                width,
                height,
                mimetype: "image/jpeg",
            });
        this.close();
    }

    /** The system camera / camera roll. Also the iOS escape hatch. */
    chooseFile() {
        this.fileRef.el && this.fileRef.el.click();
    }

    async onFilePicked(ev) {
        const file = ev.target.files && ev.target.files[0];
        ev.target.value = ""; // so picking the same file twice still fires
        if (!file) {
            return;
        }
        try {
            const b64 = await this._fileToB64(file);
            this.props.onCaptured &&
                this.props.onCaptured(b64, {
                    width: 0,
                    height: 0,
                    mimetype: file.type || "image/jpeg",
                    name: file.name || "",
                });
            this.close();
        } catch (err) {
            this.state.error = _t("读取所选图片失败，请重试。");
        }
    }

    _fileToB64(file) {
        return new Promise((resolve, reject) => {
            const fr = new FileReader();
            fr.onload = () => resolve(String(fr.result).split(",")[1] || "");
            fr.onerror = reject;
            fr.readAsDataURL(file);
        });
    }

    /** Close the camera and give the voice loop its hearing back. */
    close() {
        this._stopDecoding();
        this._releaseStream();
        const release = this._releaseHandoff;
        this._releaseHandoff = null;
        release && release();
        this.props.onClose && this.props.onClose();
    }

    /** Spoken line for a status change, without the mic hearing it. */
    say(text) {
        speakGuarded(text);
    }
}
