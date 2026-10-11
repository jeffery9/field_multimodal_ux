/** @odoo-module **/

import { registry } from "@web/core/registry";

/**
 * Non-invasive current-context service (FR-007/FR-009, extended in v3.1).
 *
 * Reads the active action / model / record from the router hash instead of
 * patching Odoo core services, so the core client stays untouched
 * (PRD §8.1: no invasive modification of native modules).
 *
 * v3.1 adds the POSITION SIGNAL — "where in the web client is the
 * operator standing?" — which the backend turns into candidate scenarios.
 * Three rules shape the addition:
 *
 *   1. The client reports RAW EVIDENCE, never a verdict. It sends the
 *      action xmlid and the model on screen; it never sends "the scenario
 *      is warehouse". If it could, the browser would be choosing the
 *      capability whitelist its own later answers are validated against.
 *
 *   2. The client sends `signal` only when it has somewhere to look.
 *      A caller that was never given signals sends NOTHING (`undefined`),
 *      which the server reads as "older client, not applicable" and
 *      answers exactly as v3.0 did. Turning that into `{}` would make
 *      every existing client suddenly start interrogating the operator.
 *
 *   3. GEOLOCATION IS OPT-IN FROM THE SERVER. The client never asks the
 *      browser for a position on its own initiative; it asks only when
 *      the backend's `geo_collect` flag says so, and only once per
 *      session. A permission prompt is a user-visible event, and an
 *      addon that triggers one to "see if it works" is a bug.
 */

/** The action/menu currently mounted, as an xmlid, or null. */
function _xmlidOf(actionService, key) {
    try {
        const action = actionService && actionService.currentController;
        if (!action) {
            return null;
        }
        return (action.props && action.props[key]) || null;
    } catch (err) {
        return null;
    }
}

export const mmuxContextService = {
    dependencies: ["router", "user"],
    start(env, { router, user }) {
        // Remembers the last position across signals so a signal that has
        // no new position still reports the one we already have, rather
        // than making the server think the operator moved off the grid.
        let cachedGeo = null;
        // A session flag: a geolocation permission prompt is a
        // user-visible event, and repeating it on every turn would be
        // harassment. One attempt per page load.
        let geoAttempted = false;

        function _readGeo(collect) {
            if (!collect || geoAttempted || cachedGeo) {
                return cachedGeo;
            }
            if (typeof navigator === "undefined" ||
                !navigator.geolocation ||
                typeof navigator.geolocation.getCurrentPosition !== "function") {
                return null;
            }
            geoAttempted = true;
            try {
                // Asynchronous by nature. We deliberately do NOT block the
                // signal on it: the position is weight-0 evidence, so
                // waiting for a slow GPS fix to attach something the
                // server will not act on would be a poor trade.
                navigator.geolocation.getCurrentPosition(
                    (pos) => {
                        cachedGeo = {
                            lat: pos.coords.latitude,
                            lng: pos.coords.longitude,
                            accuracy: pos.coords.accuracy,
                        };
                    },
                    () => { /* denied / unavailable: carry on without it */ },
                    { enableHighAccuracy: false, timeout: 4000,
                      maximumAge: 60000 }
                );
            } catch (err) {
                return null;
            }
            return cachedGeo;
        }

        return {
            getContext() {
                const current = router.current || {};
                const hash = current.hash || {};
                const resModel = hash.model || null;
                let resId = null;
                const action = hash.action;
                if (typeof action === "string" && /^\d+$/.test(action)) {
                    resId = hash.id && parseInt(hash.id, 10) || null;
                }
                return {
                    res_model: resModel,
                    res_id: resId,
                    active_id: hash.id ? parseInt(hash.id, 10) || null : null,
                    uid: user.userId,
                    companies: user.allowedCompanies,
                };
            },

            /**
             * The position signal for the agent loop.
             *
             * `collect` is the SERVER's `geo_collect` flag; defaulting it
             * to false means a caller that forgets to pass it gets the
             * conservative behaviour rather than a location request.
             *
             * Returns `null` when there is nothing to report at all — the
             * caller then omits `signal` entirely, which is what keeps an
             * old client's behaviour intact. Returning `{}` here would be
             * a different statement ("I looked and found nothing"), and
             * the backend deliberately treats the two differently.
             */
            getSignal(options) {
                const opts = options || {};
                const ctx = this.getContext();
                const actionXmlid = opts.action_xmlid ||
                    _xmlidOf(opts.actionService, "actionId") || null;
                const menuXmlid = opts.menu_xmlid || null;

                const signal = {};
                if (ctx.res_model) {
                    signal.res_model = ctx.res_model;
                }
                if (ctx.res_id) {
                    signal.res_id = ctx.res_id;
                }
                if (actionXmlid) {
                    signal.action_xmlid = actionXmlid;
                }
                if (menuXmlid) {
                    signal.menu_xmlid = menuXmlid;
                }
                const geo = _readGeo(opts.geo_collect === true);
                if (geo) {
                    signal.geo = geo;
                }

                if (!signal.res_model && !signal.action_xmlid &&
                    !signal.menu_xmlid && !signal.geo) {
                    // Nothing to report. NOT `{}` — see the docstring.
                    return null;
                }
                return signal;
            },
        };
    },
};

registry.category("services").add("mmux_context", mmuxContextService);
