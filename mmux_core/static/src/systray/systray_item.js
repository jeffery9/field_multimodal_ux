/** @odoo-module **/

import { Component, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { MmuxPanel } from "../panel/panel";
import { MmuxGuide } from "../guide/guide";
import { tts } from "../services/tts";
import { voiceLoop } from "../services/voice_loop";

/**
 * Systray entry point (FR-010): toggles the field assistant panel.
 *
 * It also carries the **global wake listener**. That placement is
 * deliberate: this component is mounted for the whole session, so unlike
 * the panel it can hear the worker while the panel is closed. Without it
 * the very first action of a hands-free session -- opening the assistant
 * -- would still need a tap, which would defeat the point.
 *
 * The listener is off by default and only ever *opens* the UI. It cannot
 * execute anything: once the panel is up it runs its own loop with its
 * own consent rules, and this one stands down.
 */
export class MmuxSystrayItem extends Component {
    static components = { MmuxPanel, MmuxGuide };
    setup() {
        this.state = useState({
            open: false,
            tab: "assistant",
            wakeArmed: false,
            voiceNotice: "",
        });
        this.action = useService("action");
        onWillUnmount(() => this._disarm());
    }
    toggle() {
        this.state.open = !this.state.open;
    }
    setTab(tab) {
        this.state.tab = tab;
    }
    openWorkspace() {
        this.action.doAction({ type: "ir.actions.client", tag: "mmux_workspace" });
    }

    // ---------------------------------------------------------------- //
    // global wake listener

    /**
     * Arm a session-wide wake listener.
     *
     * Why a separate listener rather than reusing the panel's: the panel
     * only exists while it is open, so it cannot be the thing that opens
     * it. This one does exactly one job -- on the wake word, bring the
     * assistant up and hand over.
     */
    toggleWake() {
        if (this.state.wakeArmed) {
            this._disarm();
            return;
        }
        this.state.voiceNotice = "";
        const ok = voiceLoop.start({
            context: "taskloop",
            onState: () => {},
            onWake: () => this.openByVoice(),
            onError: (msg) => {
                this.state.voiceNotice = msg;
                this.state.wakeArmed = false;
            },
            // No transcript handling on purpose: this listener only opens
            // the assistant. Anything said afterwards belongs to the
            // panel's loop, which carries the real consent rules.
            onTranscript: () => {},
            onCapture: (b64, deliver) => deliver(""),
        });
        this.state.wakeArmed = ok;
        if (!ok) {
            this.state.voiceNotice = _t("此设备不支持免手模式。");
            return;
        }
        tts.speak(_t("已在后台待命，说「你好助手」打开现场助手"));
    }

    _disarm() {
        if (this.state.wakeArmed) {
            voiceLoop.stop();
            this.state.wakeArmed = false;
        }
    }

    /** The wake word opens the UI; the panel then takes over the loop. */
    openByVoice() {
        this._disarm();
        this.state.open = true;
        this.state.tab = "assistant";
    }
}
MmuxSystrayItem.template = "mmux_core.SystrayItem";

const systrayItem = {
    Component: MmuxSystrayItem,
};
registry.category("systray").add("mmux_assistant", systrayItem, { sequence: 90 });
