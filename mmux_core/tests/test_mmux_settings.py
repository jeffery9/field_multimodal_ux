# -*- coding: utf-8 -*-
"""Odoo-side tests for the settings form (v3.1).

Before this feature, configuration meant *Settings → Technical → System
parameters* — typing keys such as ``mmux.llm.base_url`` by hand. These
tests prove the properties that make the form a *safe* replacement, not
merely a prettier one:

1. **The keys did not move.** A field write lands on the very parameter
   the runtime reads, so "configured in the UI" and "configured by
   parameter" cannot disagree, and nothing needed migrating.
2. **A secret is never handed back.** ``get_values()`` must not return the
   stored API key — a `password` field masks the *rendering*, not the ORM
   value, so without an explicit guard the plaintext would travel to the
   browser.
3. **Saving cannot erase a secret by accident.** The field reads back
   blank, so a form opened and saved untouched submits an empty string;
   treating that as "clear the key" would destroy a working credential.
4. **A configuration mistake fails on the settings page** rather than at
   the moment a field worker speaks into the device.
5. **The test hooks stay out of the UI.** The mock parameters have no
   field, so they are not a documented feature an operator can switch on.
"""
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMmuxSettings(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Settings = self.env["res.config.settings"]
        self.Param = self.env["ir.config_parameter"].sudo()
        self.service = self.env["mmux.service"]

    def _settings(self, **vals):
        rec = self.Settings.create(vals)
        rec.set_values()
        return rec

    # ---------------------------------------------------------------- #
    # 1. the keys are the same keys
    # ---------------------------------------------------------------- #
    def test_a_field_write_lands_on_the_parameter_the_runtime_reads(self):
        self._settings(mmux_llm_model="gpt-4o-mini-test")
        self.assertEqual(
            self.Param.get_param("mmux.llm.model"), "gpt-4o-mini-test")

    def test_every_settings_field_points_at_a_mmux_parameter(self):
        """The field/parameter map, asserted rather than assumed.

        If someone later renames a field's ``config_parameter``, the form
        would silently stop configuring anything; this test is the tripwire.
        """
        expected = {
            "mmux_llm_provider": "mmux.llm.provider",
            "mmux_llm_base_url": "mmux.llm.base_url",
            "mmux_llm_model": "mmux.llm.model",
            "mmux_llm_timeout": "mmux.llm.timeout",
            "mmux_asr_provider": "mmux.asr.provider",
            "mmux_asr_base_url": "mmux.asr.base_url",
            "mmux_asr_model": "mmux.asr.model",
            "mmux_asr_language": "mmux.asr.language",
            "mmux_asr_timeout": "mmux.asr.timeout",
            "mmux_vision_provider": "mmux.vision.provider",
            "mmux_vision_model": "mmux.vision.model",
            "mmux_ocr_provider": "mmux.ocr.provider",
            "mmux_agent_enabled": "mmux.agent.enabled",
        }
        for field_name, param_key in expected.items():
            field = self.Settings._fields.get(field_name)
            self.assertIsNotNone(field, f"{field_name} is not a field")
            self.assertEqual(
                field.config_parameter, param_key,
                f"{field_name} no longer writes to {param_key}")

    def test_the_api_key_fields_have_NO_config_parameter(self):
        """The secrets must stay OUT of the framework's config bucket.

        `default_get()` populates every field carrying a `config_parameter`
        from its parameter, and it does so *before* merging `get_values()`.
        A secret declared the ordinary way would therefore be written into
        the form payload by the framework, and withholding it in
        `get_values()` would be too late. Declaring no parameter is what
        actually keeps it out.
        """
        for field_name in ("mmux_llm_api_key", "mmux_asr_api_key"):
            field = self.Settings._fields[field_name]
            self.assertTrue(field.password,
                            f"{field_name} should render as a password")
            self.assertFalse(
                field.config_parameter,
                f"{field_name} must not declare config_parameter")

    # ---------------------------------------------------------------- #
    # 2. migration: legacy parameters are simply visible
    # ---------------------------------------------------------------- #
    def test_a_parameter_set_the_old_way_appears_in_the_form(self):
        """An installation that configured by parameter keeps working.

        There is no migration step to get wrong, because the value never
        moved: the form reads the same key the old workflow wrote.
        """
        self.Param.set_param("mmux.llm.provider", "mock")
        self.Param.set_param("mmux.llm.model", "legacy-model")
        values = self.Settings.get_values()
        self.assertEqual(values.get("mmux_llm_provider"), "mock")
        self.assertEqual(values.get("mmux_llm_model"), "legacy-model")

    def test_saving_the_form_immediately_changes_what_the_service_reads(self):
        self._settings(mmux_llm_provider="mock",
                       mmux_llm_base_url="http://localhost:9999/v1")
        config = self.service._provider_config()
        self.assertEqual(config["mmux.llm.provider"], "mock")
        self.assertEqual(config["mmux.llm.base_url"], "http://localhost:9999/v1")

    # ---------------------------------------------------------------- #
    # 3. secrets are write-only
    # ---------------------------------------------------------------- #
    def test_a_stored_key_is_never_returned_to_the_browser(self):
        """The core of the write-only promise.

        `password="True"` masks the widget, not the payload. The secret is
        kept out of the form by declaring NO `config_parameter` on the
        field, so `default_get()` never populates it — checking the whole
        payload, not just `get_values()`, is what makes this meaningful.
        """
        self.Param.set_param("mmux.llm.api_key", "sk-secret-value")
        payload = self.Settings.default_get(
            list(self.Settings.fields_get()))
        self.assertNotIn("sk-secret-value", str(payload))
        self.assertFalse(payload.get("mmux_llm_api_key"))

    def test_saving_with_a_blank_key_does_not_erase_the_stored_one(self):
        """A blank field means "leave it alone", not "delete it".

        The field is blanked on read, so an untouched form submits an empty
        string. If that were written through, opening settings and pressing
        Save would silently wipe a working credential.
        """
        self.Param.set_param("mmux.llm.api_key", "sk-keep-me")
        self._settings(mmux_llm_api_key=False)
        self.assertEqual(self.Param.get_param("mmux.llm.api_key"), "sk-keep-me")

    def test_a_new_key_overwrites_the_stored_one(self):
        self.Param.set_param("mmux.llm.api_key", "sk-old")
        self._settings(mmux_llm_api_key="sk-new")
        self.assertEqual(self.Param.get_param("mmux.llm.api_key"), "sk-new")

    def test_the_form_says_whether_a_key_is_stored(self):
        """The booleans that replace the masked value.

        Since the input always renders empty, "configured" and
        "not configured" would look identical without this indicator.
        """
        self.Param.set_param("mmux.llm.api_key", False)
        self.Param.set_param("mmux.asr.api_key", False)
        rec = self.Settings.create({})
        self.assertFalse(rec.mmux_llm_api_key_set)
        self.assertFalse(rec.mmux_asr_api_key_set)

        self.Param.set_param("mmux.llm.api_key", "sk-x")
        rec2 = self.Settings.create({})
        self.assertTrue(rec2.mmux_llm_api_key_set)
        self.assertFalse(rec2.mmux_asr_api_key_set)

    # ---------------------------------------------------------------- #
    # 4. a mistake fails here, not in the field
    # ---------------------------------------------------------------- #
    def test_a_timeout_outside_the_supported_range_is_refused(self):
        with self.assertRaises(ValidationError):
            self.Settings.create({"mmux_llm_timeout": 1})
        with self.assertRaises(ValidationError):
            self.Settings.create({"mmux_llm_timeout": 99999})

    def test_a_supported_timeout_is_accepted(self):
        self._settings(mmux_llm_timeout=30, mmux_asr_timeout=15)
        self.assertEqual(self.Param.get_param("mmux.llm.timeout"), "30")

    def test_an_unregistered_provider_is_refused(self):
        """A typo is caught here rather than surfacing as a ProviderError
        the first time someone speaks into the device."""
        with self.assertRaises(ValidationError):
            self.Settings.create({"mmux_llm_provider": "not_a_provider"})

    def test_a_registered_provider_is_accepted(self):
        self._settings(mmux_llm_provider="mock")
        self.assertEqual(self.Param.get_param("mmux.llm.provider"), "mock")

    def test_an_empty_registry_does_not_lock_the_field(self):
        """`mmux.ocr.provider` has no registered provider today.

        A whitelist check whose list is empty is not validation, it is a
        lock — every value would be rejected and the field could never be
        configured at all. Validation stands down when there is nothing to
        validate against.
        """
        from odoo.addons.mmux_core.services.mmux_core import PROVIDER_REGISTRY
        if PROVIDER_REGISTRY.names("ocr"):
            self.skipTest("an ocr provider is registered in this build")
        self._settings(mmux_ocr_provider="some-future-ocr")
        self.assertEqual(
            self.Param.get_param("mmux.ocr.provider"), "some-future-ocr")

    # ---------------------------------------------------------------- #
    # 5. the test hooks are not a feature
    # ---------------------------------------------------------------- #
    def test_the_mock_parameters_have_no_settings_field(self):
        """`mock_response` supplies a provider's RESPONSE BODY.

        Turning it into a labelled admin field would promote a test hook
        into a supported feature — and a way to make the platform say
        anything. It must stay unreachable from the settings page.
        """
        for param_key in ("mmux.llm.mock_response", "mmux.llm.mock_error",
                          "mmux.asr.mock_transcript"):
            owners = [
                f.name for f in self.Settings._fields.values()
                if getattr(f, "config_parameter", None) == param_key
            ]
            self.assertFalse(
                owners, f"{param_key} is exposed as settings field {owners}")

    def test_the_mock_parameters_still_work_for_tests(self):
        """No field means the framework never touches them, so the test
        suite can keep injecting responses exactly as before."""
        self.Param.set_param("mmux.llm.provider", "mock")
        self.Param.set_param("mmux.llm.mock_response", '{"x": 1}')
        self.assertEqual(
            self.Param.get_param("mmux.llm.mock_response"), '{"x": 1}')


@tagged("post_install", "-at_install")
class TestMmuxSettingsProviderOptions(TransactionCase):
    """The provider dropdowns are derived from the live registry."""

    def setUp(self):
        super().setUp()
        self.Settings = self.env["res.config.settings"]

    def test_the_options_come_from_the_registry(self):
        from odoo.addons.mmux_core.services.mmux_core import PROVIDER_REGISTRY
        for kind, field_name in (("llm", "mmux_llm_provider"),
                                 ("asr", "mmux_asr_provider"),
                                 ("vision", "mmux_vision_provider")):
            options = dict(
                self.Settings._fields[field_name]._description_selection(
                    self.env))
            self.assertIn("mock", options,
                          f"{kind} should always offer mock")
            for name in PROVIDER_REGISTRY.names(kind):
                self.assertIn(
                    name, options,
                    f"{kind} provider {name} is registered but not offered")

    def test_mock_is_always_offered(self):
        """Even where no real provider is registered, mock must be
        selectable — it is what the test suite and offline demos use."""
        options = dict(
            self.Settings._fields["mmux_llm_provider"]
            ._description_selection(self.env))
        self.assertIn("mock", options)
