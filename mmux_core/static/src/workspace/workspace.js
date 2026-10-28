/**
 * Copyright (C) 2026 jeffery9@gmail.com & geninit
 * License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
 */
/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { mmuxApi } from "../services/mmux_api";
import { MmuxPanel } from "../panel/panel";
import { MmuxTaskLoop } from "../taskloop/taskloop";

/**
 * Task-oriented workspace client action (FR-011/FR-012).
 *
 * Two tabs:
 *  - **任务循环** (default): fetch a task -> do it with guided help ->
 *    report it done -> the system publishes the next one. This is the
 *    workflow the platform is built around.
 *  - **多模态输入**: the free-form panel (voice/text/barcode/photo) for
 *    ad-hoc interactions outside the loop.
 *
 * Scenario-specific behavior comes from configuration, not from
 * duplicated frontends.
 */
export class MmuxWorkspace extends Component {
    static components = { MmuxPanel, MmuxTaskLoop };
    static template = "mmux_core.Workspace";
    setup() {
        this.orm = useService("orm");
        this.state = useState({
            scenarios: [],
            activeScenario: "",
            tab: "loop",             // "loop" | "panel"
        });
        onWillStart(async () => {
            this.state.scenarios = await this.orm.searchRead(
                "mmux.scenario.config", [["active", "=", true]],
                ["name", "code", "default_input", "description"]
            );
            if (this.state.scenarios.length) {
                this.state.activeScenario = this.state.scenarios[0].code;
            }
        });
    }
    selectScenario(ev) {
        this.state.activeScenario = ev.target.value;
    }
    setTab(tab) {
        this.state.tab = tab;
    }
    get activeScenarioRecord() {
        return this.state.scenarios.find((s) => s.code === this.state.activeScenario);
    }
}

registry.category("actions").add("mmux_workspace", MmuxWorkspace, { force: true });
