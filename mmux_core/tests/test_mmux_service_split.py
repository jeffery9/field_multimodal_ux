# -*- coding: utf-8 -*-
"""The `mmux.service` split must stay behaviourally invisible.

Since v3.1.1 the service is spread over several files that all declare
`_inherit = "mmux.service"`.  Odoo merges them into ONE model, so the only
thing a caller can observe is the merged model -- which is exactly what
these tests pin down.

The point is not to enumerate methods (that would just restate the source).
It is to fail loudly if the split ever stops being transparent: if a file
is dropped from `models/__init__.py`, or a sibling forgets `_inherit`, the
methods silently disappear from `env["mmux.service"]` and only these
assertions would notice.
"""
from odoo.tests import TransactionCase, tagged


# One representative method per split file.  If a file stops being imported
# or stops inheriting, its entry disappears from the merged model.
REPRESENTATIVES = {
    "mmux_service_infra.py": "_provider_config",
    "mmux_service_skill.py": "_user_lang",
    "mmux_service_capability.py": "execute_capability",
    "mmux_service_scenario.py": "_scenario_resolve",
    "mmux_service_loop.py": "agent_turn",
    "mmux_service_flow.py": "task_next",
    "mmux_service_plan.py": "propose_plan",
}

# The contract the controllers and the frontend actually call.  Kept as a
# literal list on purpose: it is an external interface, so removing or
# renaming one of these is a breaking change that must be deliberate.
PUBLIC_API = (
    "parse_intent", "execute_capability", "resolve_barcode",
    "capabilities_for_user",
    "plan_run", "propose_plan", "resolve_binding",
    "guide_flows", "guide_start", "guide_current", "guide_advance",
    "set_skill_level",
    "task_next", "task_current", "task_guide", "task_help", "task_report",
    "agent_start", "agent_turn",
    "transcribe", "analyze_image",
)


@tagged("post_install", "-at_install", "mmux")
class TestMmuxServiceSplit(TransactionCase):

    def test_every_split_file_contributes_to_the_one_model(self):
        """Each file's methods are reachable on the merged service."""
        Service = self.env["mmux.service"]
        missing = sorted(
            f"{fname}:{meth}" for fname, meth in REPRESENTATIVES.items()
            if not hasattr(Service, meth)
        )
        self.assertFalse(
            missing,
            "these split files are not reaching the merged model -- check "
            "models/__init__.py and the `_inherit` on each file: "
            + ", ".join(missing))

    def test_there_is_exactly_one_service_model(self):
        """The siblings inherit; they must not each declare their own model."""
        model = self.env["ir.model"].sudo().search([("model", "=", "mmux.service")])
        self.assertEqual(len(model), 1,
                         "mmux.service must be a single model, not one per file")

    def test_the_public_api_is_intact(self):
        """The call surface the controllers/frontend use did not move."""
        Service = self.env["mmux.service"]
        missing = [name for name in PUBLIC_API if not hasattr(Service, name)]
        self.assertFalse(missing,
                         "public service methods vanished in the split: "
                         + ", ".join(missing))

    def test_no_sibling_relabels_the_model(self):
        """A sibling must carry `_inherit` only, never a new `_name`.

        If one accidentally declared `_name = "mmux.service.agent"` it would
        become a separate model and its methods would silently not merge.
        """
        Service = self.env["mmux.service"]
        self.assertEqual(Service._name, "mmux.service")
        # the base file is the only one that may set `_name`
        self.assertTrue(Service._description,
                        "the merged model must still carry a _description")
