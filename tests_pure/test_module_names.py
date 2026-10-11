# -*- coding: utf-8 -*-
"""Regression guard: module-level names must be bound where they are used.

Why this file exists
--------------------
`task_report()` in `mmux_service_flow.py` called `fields.Datetime.now()`
while the file imported only `api, models` from `odoo`.  Every successful
task completion therefore raised `NameError` -- not "sometimes", always,
on the success path.  It shipped because every layer of verification had
a blind spot at once:

* `check_standards.py` rule 10 guards unimported *stdlib* modules; its
  name list did not contain `fields`, so the rule was correct but blind.
* `tests_pure` never imports the Odoo model files.
* the Odoo tests are tagged `post_install, -at_install` and the
  simulator in `/tmp` only loads `test_mmux_scenario_context.py`, so the
  ~10 `task_report` call sites had never actually executed.

This test is the layer that closes the gap.  It is deliberately a static
check: it can see every file, runs without a server, and cannot be
skipped by a tag.  The rule and this test must agree -- if one is
tightened, tighten the other.

The point is not `fields` specifically.  The point is that a method body
can be moved between the split `mmux_service_*` files while its imports
stay behind, and that class of mistake is invisible to any test that
only exercises a different file.
"""
import ast
import pathlib
import unittest

# The same closed list `tools/check_standards.py` guards.  Kept in sync
# on purpose: a failure here names the file, while the standards run
# reports it as one of its 13 rules.
GUARDED_NAMES = (
    "json", "re", "base64", "hashlib", "datetime", "random",
    "itertools", "functools", "collections", "logging", "math",
    "uuid", "time", "os", "csv", "io",
    "fields", "tools", "http", "Command", "SUPERUSER_ID",
)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _module_dirs():
    """Every shipped module directory (the same set the tool checks)."""
    return sorted(p for p in ROOT.glob("mmux_*") if p.is_dir())


def _bound_names(tree):
    """Names bound at module level, plus function/class/comprehension
    targets.

    A bare `fields` may legitimately be a parameter -- `ParamSchema.
    __init__(self, fields)` does exactly that.  Only a name that is used
    but bound *nowhere* is a defect, so the check has to know about all
    the ordinary ways a name gets bound, not just `import`.
    """
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.alias):
            bound.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return bound


def _attribute_bases(tree):
    """Names used as the base of an attribute access (`X.y`).

    This is the missing-import signature.  A bare `Name` is NOT included:
    it is far more often a local or a parameter, and matching it turned
    `ParamSchema.__init__(self, fields)` into a false positive.
    """
    used = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)):
            used.add(node.value.id)
    return used


class TestModuleNamesAreBound(unittest.TestCase):
    """Every `X.y` in a shipped file must have X bound in that file."""

    def _files(self):
        for module in _module_dirs():
            for path in sorted(module.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                yield path

    def test_no_guarded_name_is_used_without_being_bound(self):
        offenders = []
        for path in self._files():
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue          # the parse sweep reports syntax errors
            bound = _bound_names(tree)
            for name in _attribute_bases(tree) & set(GUARDED_NAMES):
                if name not in bound:
                    offenders.append(
                        "%s: `%s.` is used but never bound"
                        % (path.relative_to(ROOT), name))
        self.assertEqual(
            offenders, [],
            "unbound module references (a NameError waiting to happen):\n  "
            + "\n  ".join(offenders))

    def test_the_check_can_actually_fail(self):
        """Self-test: the detector must flag a known-bad source.

        Without this, a typo that made `_attribute_bases` return the
        empty set would turn the test above into a permanent no-op that
        still reports green -- the most dangerous kind of passing test.
        """
        bad = "def f(x):\n    return fields.Datetime.now()\n"
        tree = ast.parse(bad)
        self.assertEqual(_attribute_bases(tree), {"fields"})
        self.assertNotIn("fields", _bound_names(tree))

    def test_a_parameter_named_like_a_module_is_not_flagged(self):
        """The false positive that shaped this rule, pinned.

        `ParamSchema.__init__(self, fields)` in schema.py binds `fields`
        as a parameter.  `self.fields` is then an attribute access on an
        object, not on the module, and must never be reported.
        """
        src = ("class S:\n"
               "    def __init__(self, fields):\n"
               "        self.fields = fields\n"
               "    def go(self):\n"
               "        return self.fields.items()\n")
        tree = ast.parse(src)
        self.assertIn("fields", _bound_names(tree))
        # `self.fields` -> base is `self`, so `fields` is not a base
        self.assertNotIn("fields", _attribute_bases(tree))

    def test_it_finds_the_real_shipped_files(self):
        """Guard against the glob silently matching nothing."""
        found = list(self._files())
        self.assertGreater(len(found), 50, "expected to scan many files")
        names = {p.name for p in found}
        self.assertIn("mmux_service_flow.py", names)
        self.assertIn("schema.py", names)


if __name__ == "__main__":
    unittest.main()
