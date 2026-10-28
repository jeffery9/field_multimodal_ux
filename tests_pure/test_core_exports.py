# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Guard the barrel package's public surface (`mmux_core/__init__.py`).

Why this file exists
--------------------
The review of `__init__.py` found two regressions that no existing test
could see, and both come from the same structural weakness: the barrel
re-exports names from many submodules, and nothing checked what the
barrel actually binds.

1. `taskqueue.py` and `agentloop.py` each define an unrelated
   ``LOOP_STATES`` tuple. Importing both under the same name into the
   barrel silently shadowed one with the other (last import wins), so
   every model doing ``from ..services.mmux_core import LOOP_STATES``
   received whichever tuple happened to be imported second — a semantic
   mismatch invisible to any single-module test.

2. A new action constant (``ACT_PERCEIVE_IMAGE``) was added to
   ``agentloop.AGENT_ACTIONS`` but never re-exported through the barrel,
   breaking the "complete public API" pattern the barrel follows for
   every other module.

These tests are static in spirit but real-import based: they run without
Odoo (the core must stay pure), take ~milliseconds, and fail *at the
exact moment* either class of mistake is reintroduced.
"""
import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "mmux_core" / "services"))

import mmux_core  # noqa: E402
from mmux_core import agentloop, taskqueue  # noqa: E402

INIT = (pathlib.Path(__file__).resolve().parent.parent
        / "mmux_core" / "services" / "mmux_core" / "__init__.py")


class TestLoopStatesShadowing(unittest.TestCase):
    """The two unrelated LOOP_STATES tuples must stay distinguishable."""

    def test_submodule_tuples_are_different(self):
        """The premise of the whole guard: same NAME, different content."""
        self.assertNotEqual(taskqueue.LOOP_STATES, agentloop.LOOP_STATES)

    def test_barrel_exposes_both_under_distinct_names(self):
        self.assertEqual(mmux_core.TASK_LOOP_STATES, taskqueue.LOOP_STATES)
        self.assertEqual(mmux_core.AGENT_LOOP_STATES, agentloop.LOOP_STATES)

    def test_plain_loop_states_is_the_agent_vocabulary(self):
        """The Odoo models and the frontend contract tests import the bare
        ``LOOP_STATES`` expecting the AGENT loop vocabulary; pinning it
        here means reordering the imports in ``__init__.py`` cannot flip
        which tuple downstream consumers silently receive."""
        self.assertEqual(mmux_core.LOOP_STATES, agentloop.LOOP_STATES)

    def test_no_duplicate_binding_name_in_imports(self):
        """A name bound twice by top-level imports is a shadowing bug
        waiting to happen (that is exactly how LOOP_STATES broke)."""
        tree = ast.parse(INIT.read_text(encoding="utf-8"))
        bound = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound.append(alias.asname or alias.name)
        dupes = {n for n in bound if bound.count(n) > 1}
        self.assertEqual(dupes, set(),
                         f"barrel binds {sorted(dupes)} more than once")


class TestBarrelApiSurface(unittest.TestCase):
    """Every public submodule name must be reachable through the barrel."""

    SUBMODULES = ("schema", "capability", "provider", "guard", "intent",
                  "quantity", "idempotency", "plan", "flow", "taskqueue",
                  "agent", "agentloop", "i18n", "scenario")

    def _public_names(self, module):
        """Names the submodule itself defines (not imports it happens to
        host) — those are its API surface; imported symbols like `Dict`
        or sibling-module constants are not.

        Module-level singletons bound to an instance of a class defined in
        the same module (`REGISTRY = ProviderRegistry()`) report the
        *class's* ``__module__`` via ``type().__module__``, so they need an
        explicit check: they ARE part of the public surface.
        """
        names = set()
        for n, v in vars(module).items():
            if n.startswith("_"):
                continue
            owner = getattr(v, "__module__", None)
            if owner is None and v.__class__.__module__ == module.__name__:
                owner = module.__name__  # module-level singleton instance
            if owner == module.__name__:
                names.add(n)
        return names

    def test_all_names_are_bound(self):
        """Every name in `__all__` must actually be bound on the package;
        a stale entry turns `from mmux_core import *` into an
        AttributeError at import time."""
        declared = set(mmux_core.__all__)
        self.assertEqual(len(declared), len(mmux_core.__all__),
                         "duplicate entry in __all__")
        unbound = sorted(n for n in declared if not hasattr(mmux_core, n))
        self.assertEqual(unbound, [],
                         f"__all__ advertises unbound names: {unbound}")

    def test_every_submodule_public_name_is_reexported(self):
        """The barrel is the documented public API of the pure core; a
        constant added to a submodule (e.g. ACT_PERCEIVE_IMAGE) must be
        surfaced here too, or Odoo-layer consumers silently miss it."""
        missing = {}
        for name in self.SUBMODULES:
            module = __import__("mmux_core." + name, fromlist=[name])
            gone = sorted(self._public_names(module)
                          - set(mmux_core.__all__)
                          # LOOP_STATES exists in two unrelated modules;
                          # the barrel re-exports it aliased (guarded by
                          # TestLoopStatesShadowing above).
                          - {"LOOP_STATES"}
                          )
            if gone:
                missing[name] = gone
        self.assertEqual(missing, {},
                         "submodule names not exported by the barrel")


if __name__ == "__main__":
    unittest.main()
