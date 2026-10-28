# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Provider & feature settings, surfaced as a form instead of raw parameters.

Before this model existed, configuring the platform meant opening
*Settings → Technical → System parameters* and typing keys such as
``mmux.llm.base_url`` by hand. That is a developer interface: it offers no
validation, no labels, no defaults, and no way for a non-developer to
discover what is configurable at all. This model puts the same values
behind a normal Odoo settings page.

Three properties are deliberate.

**The parameter keys are unchanged.** Every field below declares
``config_parameter="mmux...."`` — the *same* key the rest of the code
already reads. Odoo writes the field's value to that key on save. Two
consequences follow, and both matter:

* there is no migration. Existing installations keep working, because the
  value never moved;
* there is no "the settings page changed it but the runtime did not notice"
  class of bug, because there is only ever one copy of the value.

**The mock/test parameters are NOT here.** ``mmux.llm.mock_response``,
``mmux.llm.mock_error`` and ``mmux.asr.mock_transcript`` are test-injection
channels. ``mock_response`` in particular is an entry point for supplying a
provider's *response body*; promoting it to a labelled admin field would be
turning a test hook into a documented feature. Having no field means the
framework never touches those keys, so the test suite can keep setting them
directly.

**API keys are write-only.** See ``get_values`` / ``set_values`` below —
this is the part most easily got wrong.
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..services.mmux_core import PROVIDER_REGISTRY

# The lower bound mirrors `_timeout()` in the AI provider adapter, which
# clamps with `max(3, ...)`. Previously an operator could type 1 and the
# value was silently raised to 3; a setting that quietly ignores what you
# typed is worse than one that refuses it, so the clamp is promoted to an
# explicit error here.
MIN_TIMEOUT = 3
MAX_TIMEOUT = 3600


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ------------------------------------------------------------------ #
    # semantic recognition (the LLM is the ONLY semantic path)
    # ------------------------------------------------------------------ #
    mmux_llm_provider = fields.Selection(
        selection=lambda self: self._provider_selection("llm"),
        string="Semantic provider",
        config_parameter="mmux.llm.provider",
        help="Provider used for intent recognition and task selection. "
             "'mock' returns deterministic canned answers and never touches "
             "the network — it is what the test suite uses.")
    mmux_llm_base_url = fields.Char(
        "Base URL", config_parameter="mmux.llm.base_url",
        help="Any OpenAI-compatible endpoint. Point this at a self-hosted "
             "server (LocalAI / vLLM) to keep utterances on-premise.")
    mmux_llm_api_key = fields.Char("API key", password="True")
    mmux_llm_model = fields.Char(
        "Model", config_parameter="mmux.llm.model")
    mmux_llm_timeout = fields.Integer(
        "Timeout (s)", config_parameter="mmux.llm.timeout",
        help="Seconds to wait before the provider is treated as "
             "unavailable. Between %s and %s." % (MIN_TIMEOUT, MAX_TIMEOUT))

    # ------------------------------------------------------------------ #
    # speech
    # ------------------------------------------------------------------ #
    mmux_asr_provider = fields.Selection(
        selection=lambda self: self._provider_selection("asr"),
        string="Speech provider",
        config_parameter="mmux.asr.provider")
    mmux_asr_base_url = fields.Char(
        "Base URL", config_parameter="mmux.asr.base_url")
    mmux_asr_api_key = fields.Char("API key", password="True")
    mmux_asr_model = fields.Char(
        "Model", config_parameter="mmux.asr.model")
    mmux_asr_language = fields.Char(
        "Language", config_parameter="mmux.asr.language",
        help="Leave empty to let the provider auto-detect. Field workers "
             "mix languages mid-sentence, so detection is usually the "
             "better default.")
    mmux_asr_timeout = fields.Integer(
        "Timeout (s)", config_parameter="mmux.asr.timeout")

    # ------------------------------------------------------------------ #
    # vision / OCR
    # ------------------------------------------------------------------ #
    mmux_vision_provider = fields.Selection(
        selection=lambda self: self._provider_selection("vision"),
        string="Vision provider",
        config_parameter="mmux.vision.provider")
    mmux_vision_model = fields.Char(
        "Vision model", config_parameter="mmux.vision.model")
    # NOTE: `mmux.ocr.provider` is read by `analyze_image()` but no
    # `kind="ocr"` provider ships today, so the registry legitimately has
    # nothing to offer. The field is therefore a Char rather than a
    # Selection with an empty option list, and the validator below stands
    # down when the registry is empty. See `_check_provider_names`.
    mmux_ocr_provider = fields.Char(
        "OCR provider", config_parameter="mmux.ocr.provider",
        help="Used when an image is analysed for OCR. Falls back to the "
             "vision provider when empty.")

    # ------------------------------------------------------------------ #
    # agent loop
    # ------------------------------------------------------------------ #
    mmux_agent_enabled = fields.Boolean(
        "Enable the agent loop", config_parameter="mmux.agent.enabled",
        default=True,
        help="When off, the agent loop is not offered at all and the "
             "guided task flow is the only path.")
    mmux_agent_max_turns = fields.Integer(
        "Max reasoning turns per loop",
        config_parameter="mmux.agent.max_turns",
        help="Hard budget: a loop that would exceed this many reasoning "
             "turns terminates safely (the operator is told). Tightening "
             "this ends over-budget IN-PROGRESS loops on their next turn — "
             "the safe direction. Empty = module default (6).")
    mmux_agent_max_capability_calls = fields.Integer(
        "Max capability proposals per loop",
        config_parameter="mmux.agent.max_capability_calls",
        help="Hard budget on capability *proposals* per loop — a loop "
             "proposing this many writes is almost certainly stuck. Empty "
             "= module default (3).")
    mmux_agent_max_repeated_step = fields.Integer(
        "Max identical-step repeats",
        config_parameter="mmux.agent.max_repeated_step",
        help="How many times the identical step (same action + same "
             "signature) may repeat before the loop is declared stuck. "
             "Empty = module default (2).")

    # ------------------------------------------------------------------ #
    # write-only key indicators
    #
    # A `password` field always renders empty, so "there is a key stored"
    # and "there is no key stored" look identical on screen. These read-only
    # booleans make the difference visible without ever revealing the value.
    # ------------------------------------------------------------------ #
    mmux_llm_api_key_set = fields.Boolean(
        "Key stored", compute="_compute_api_key_set")
    mmux_asr_api_key_set = fields.Boolean(
        "Key stored", compute="_compute_api_key_set")

    # ------------------------------------------------------------------ #
    # selection helpers
    # ------------------------------------------------------------------ #
    @api.model
    def _provider_selection(self, kind):
        """Options for a provider field, taken from the live registry.

        Deriving the options from the registry (rather than hardcoding
        them) means a newly registered provider becomes selectable with no
        change here. `mock` is always offered for every kind.
        """
        names = set(PROVIDER_REGISTRY.names(kind)) or set()
        names.add("mock")
        return [(n, n) for n in sorted(names)]

    @api.depends_context("mmux_key_probe")
    def _compute_api_key_set(self):
        Param = self.env["ir.config_parameter"].sudo()
        for rec in self:
            rec.mmux_llm_api_key_set = bool(
                Param.get_param("mmux.llm.api_key", ""))
            rec.mmux_asr_api_key_set = bool(
                Param.get_param("mmux.asr.api_key", ""))

    # ------------------------------------------------------------------ #
    # write-only handling for the API keys
    #
    # These two parameters are deliberately NOT declared with
    # `config_parameter=`. That attribute would make the framework treat
    # them as ordinary config fields, and `default_get()` populates every
    # such field straight from its parameter — *before* merging
    # `get_values()`. So popping the key in `get_values()` would not be
    # enough: the framework would have already put the plaintext back.
    # (See `_get_classified_fields` and `default_get` in
    # `odoo/addons/base/models/res_config.py`: a field joins the 'config'
    # bucket purely on the presence of `config_parameter`.)
    #
    # Omitting it keeps the secret out of the framework's loop entirely,
    # which leaves exactly one place that can read it and one that can
    # write it — both below. The parameter keys themselves are unchanged.
    # ------------------------------------------------------------------ #
    _SECRETS = (("mmux_llm_api_key", "mmux.llm.api_key"),
                ("mmux_asr_api_key", "mmux.asr.api_key"))

    def get_values(self):
        """Never hand a stored secret back to the browser.

        `password="True"` masks the *rendering*; the ORM still returns the
        plaintext, and anything `get_values()` returns lands in the form
        payload — readable in the page source and in the network response.
        The `*_api_key_set` booleans are the intended substitute: they say
        "a key is stored" without saying what it is.

        Nothing needs to be popped here, because these fields have no
        `config_parameter` and so are never populated by `default_get`.
        Returning them unset is the whole mechanism.
        """
        return super().get_values()

    def set_values(self):
        """Persist the secrets by hand, treating blank as "leave it alone".

        Two things this must get right:

        * **A blank field means "unchanged".** `get_values()` never
          repopulates the input, so a form opened and saved untouched
          submits an empty string. Writing that through would delete a
          working credential — the operator would discover it only when
          the assistant went silent.
        * **Blank when nothing is stored means "no key".** The distinction
          is whether a value already exists, which is why the stored
          parameter is consulted rather than assumed.
        """
        Param = self.env["ir.config_parameter"].sudo()
        for field_name, param_key in self._SECRETS:
            submitted = self[field_name]
            if submitted:
                # an explicit new key always wins
                Param.set_param(param_key, submitted.strip())
                continue
            if not Param.get_param(param_key, ""):
                # blank, and nothing stored: legitimately "no key yet".
                # Write nothing rather than an empty parameter.
                continue
            # blank, but a key exists: leave it untouched.
        return super().set_values()

    # ------------------------------------------------------------------ #
    # validation — fail on the settings page, not at 3am in the field
    # ------------------------------------------------------------------ #
    @api.constrains("mmux_llm_timeout", "mmux_asr_timeout")
    def _check_timeouts(self):
        for rec in self:
            for name, label in (("mmux_llm_timeout", "Semantic"),
                                ("mmux_asr_timeout", "Speech")):
                value = rec[name]
                if not value:
                    continue
                if not MIN_TIMEOUT <= value <= MAX_TIMEOUT:
                    raise ValidationError(
                        "%s timeout must be between %s and %s seconds "
                        "(got %s)." % (label, MIN_TIMEOUT, MAX_TIMEOUT, value))

    @api.constrains("mmux_agent_max_turns", "mmux_agent_max_capability_calls",
                    "mmux_agent_max_repeated_step")
    def _check_agent_budgets(self):
        """Agent budgets: fail loudly on the settings page.

        Same philosophy as `_check_timeouts`: an out-of-range budget is a
        configuration mistake, and the right moment to discover it is at
        save time — not when a worker's loop dies (or never dies) in the
        field. The service layer still falls back to the module default on
        any invalid value (defence in depth), but that path should never
        be reachable from this form.
        """
        # (field, label, upper bound); lower bound is 1 for all three —
        # a budget of 0 would make the agent unusable by construction.
        triples = (("mmux_agent_max_turns", "Max reasoning turns", 50),
                   ("mmux_agent_max_capability_calls",
                    "Max capability proposals", 50),
                   ("mmux_agent_max_repeated_step",
                    "Max identical-step repeats", 20))
        for rec in self:
            for field_name, label, upper in triples:
                value = rec[field_name]
                if value is False or value is None:
                    continue
                # NB: 0 is NOT "empty" here — `not value` would treat it
                # as unset and let it through, but a budget of 0 makes
                # the agent unusable by construction. It must be rejected.
                if not 1 <= value <= upper:
                    raise ValidationError(
                        "%s must be between 1 and %s (got %s). Leave it "
                        "empty for the module default."
                        % (label, upper, value))

    @api.constrains("mmux_llm_provider", "mmux_asr_provider",
                    "mmux_vision_provider", "mmux_ocr_provider")
    def _check_provider_names(self):
        """Reject a provider name that is not registered.

        Without this, a typo surfaces as `ProviderError` at the moment a
        field worker speaks into the device — the worst possible time to
        discover a configuration mistake.

        The check stands down when the registry offers nothing for a kind.
        A whitelist whose list is empty would not be validation, it would
        be a lock: `mmux.ocr.provider` has no registered provider today,
        and rejecting every value would make the field impossible to set.
        Validation exists to catch mistakes, not to forbid a feature.
        """
        pairs = (("mmux_llm_provider", "llm"), ("mmux_asr_provider", "asr"),
                 ("mmux_vision_provider", "vision"), ("mmux_ocr_provider", "ocr"))
        for rec in self:
            for field_name, kind in pairs:
                value = rec[field_name]
                if not value:
                    continue
                available = PROVIDER_REGISTRY.names(kind)
                if not available:
                    continue
                if value not in available and value != "mock":
                    raise ValidationError(
                        "Unknown %s provider %r. Registered providers: %s."
                        % (kind, value, ", ".join(available)))
