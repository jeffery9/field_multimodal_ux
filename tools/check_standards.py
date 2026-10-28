# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Enforce the project's coding standards automatically.

`docs/CODING_STANDARDS.md` states the rules; this script makes them
checkable. A rule that is only written down is a rule that decays -- the
point of a linter is that "we follow Odoo guidelines" becomes a pass/fail
fact rather than an intention.

The checks are deliberately narrow. Each one targets something that has a
clear right answer, because a linter that cries wolf gets disabled. Where
a rule has a documented exception (the pure core cannot import `odoo`, so
it cannot use `_()`), the exception is encoded here rather than left to
the reader's memory.

    python3 tools/check_standards.py            # check, print, exit code
    python3 tools/check_standards.py -v         # list every file examined
"""
import argparse
import ast
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------- #
# where the rules apply
# --------------------------------------------------------------------- #
# The pure core is REQUIRED not to import odoo (so it can be unit-tested
# without an Odoo runtime). It therefore cannot use `_()` for translation,
# and its Chinese literals are intentional bilingual content, not a
# missing-translation bug. Both rules below must exclude it explicitly --
# encoding the exception is the difference between a linter and a nuisance.
PURE_CORE = "mmux_core/services/mmux_core"

MODULE_DIRS = sorted(
    d for d in os.listdir(ROOT)
    if d.startswith("mmux_") and os.path.isdir(os.path.join(ROOT, d))
)

# The project's copyright attribution.  Both the manifest hygiene rule and
# the per-file header rule (see below) pin against this single constant --
# one place to change when the holder or year changes.
COPYRIGHT_HOLDER = "jeffery9@gmail.com & geninit"


def py_files(*subdirs):
    out = []
    for sub in subdirs:
        for path in glob.glob(os.path.join(ROOT, sub, "**", "*.py"),
                              recursive=True):
            if "__pycache__" in path:
                continue
            out.append(path)
    return sorted(out)


def xml_files(*subdirs):
    out = []
    for sub in subdirs:
        for path in glob.glob(os.path.join(ROOT, sub, "**", "*.xml"),
                              recursive=True):
            if "node_modules" in path:
                continue
            out.append(path)
    return sorted(out)


def rel(path):
    return os.path.relpath(path, ROOT)


class Report:
    def __init__(self):
        self.problems = []
        self.checks = 0

    def fail(self, path, line, rule, message):
        self.problems.append((rel(path), line, rule, message))

    def ok(self):
        self.checks += 1


R = Report()


# ===================================================================== #
# Rule 1 -- active record: no raw SQL
# ===================================================================== #
def rule_no_raw_sql():
    pattern = re.compile(r"_cr\.(execute|fetchall|fetchone|fetchmany)")
    for path in py_files(*MODULE_DIRS):
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            if pattern.search(line):
                R.fail(path, i, "active-record",
                       "raw SQL bypasses the ORM")
    R.ok()


# ===================================================================== #
# Rule 2 -- no print()
# ===================================================================== #
def rule_no_print():
    for path in py_files(*MODULE_DIRS):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and \
                    isinstance(node.func, ast.Name) and \
                    node.func.id == "print":
                R.fail(path, node.lineno, "odoo-guidelines",
                       "use _logger instead of print()")
    R.ok()


# ===================================================================== #
# Rule 3 -- the pure core must not import odoo
# ===================================================================== #
def rule_pure_core_is_pure():
    for path in py_files(PURE_CORE):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "odoo" or a.name.startswith("odoo."):
                        R.fail(path, node.lineno, "architecture",
                               f"pure core must not import {a.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "odoo" or mod.startswith("odoo."):
                    R.fail(path, node.lineno, "architecture",
                           f"pure core must not import {mod}")
    R.ok()


# ===================================================================== #
# Rule 4 -- no DDD vocabulary
# ===================================================================== #
# A name-level check cannot prove the absence of DDD, but these are the
# words that signal it, and their appearance warrants a human look.
DDD_NAMES = re.compile(
    r"\bclass\s+\w*(Repository|Aggregate|ValueObject|UseCase|"
    r"DomainService|UnitOfWork|DataTransferObject|DTO)\b")


def rule_no_ddd():
    for path in py_files(*MODULE_DIRS):
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            if DDD_NAMES.search(line):
                R.fail(path, i, "no-ddd",
                       "DDD construct; Odoo models already fill this role")
    R.ok()


# ===================================================================== #
# Rule 5 -- every model declares _description
# ===================================================================== #
def _model_classes(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        assigns = {}
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        assigns[t.id] = stmt.lineno
        name = None
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id == "_name":
                        if isinstance(stmt.value, ast.Constant):
                            name = stmt.value.value
        if name:
            yield node, name, assigns


def rule_models_have_description():
    for path in py_files(*MODULE_DIRS):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node, name, assigns in _model_classes(tree):
            if name.startswith("_"):
                continue  # test/mixin classes carry no name in practice
            src = ast.dump(node)
            if "_description" not in src:
                R.fail(path, node.lineno, "odoo-guidelines",
                       f"model {name} has no _description")
    R.ok()


# ===================================================================== #
# Rule 6 -- sudo() must be deliberate
# ===================================================================== #
# Not "sudo is banned": writing an audit log legitimately elevates, since
# a user who could edit their own audit trail is not audited at all. The
# rule is that elevation is *reviewable*.
#
# Two shapes of justification count:
#   1. a comment in the enclosing block, or
#   2. the elevation being structural rather than a local decision.
#
# `ELEVATED_BY_DESIGN` names the models where elevation is the intended
# design: audit rows (a user who can edit their own audit trail is not
# audited) and global configuration (a field worker must not need
# config-write rights merely to have the policy read).
#
# The model often arrives via a local alias (`Log = self.env[...]`), so
# the rule resolves simple `X = self.env["model"]` assignments first.
# Without that it flagged the alias as an unjustified elevation.
ELEVATED_BY_DESIGN = ("ir.config_parameter", "mmux.interaction.log",
                      "mmux.scenario.config")

_ALIAS_RE = re.compile(r"^\s*(\w+)\s*=\s*self\.env\[\s*[\"']([^\"']+)[\"']\s*\]")


def rule_sudo_is_justified():
    for path in py_files(*MODULE_DIRS):
        if "/tests/" in path.replace(os.sep, "/"):
            continue  # tests elevate to set up fixtures; not production code
        lines = open(path, encoding="utf-8").read().splitlines()

        # map local aliases to the model they point at
        alias = {}
        for line in lines:
            m = _ALIAS_RE.match(line)
            if m:
                alias[m.group(1)] = m.group(2)

        for i, line in enumerate(lines):
            if not re.search(r"\.sudo\(\)", line):
                continue
            named = [model for model in ELEVATED_BY_DESIGN if model in line]
            if named:
                continue
            receiver = re.findall(r"(\w+)\.sudo\(\)", line)
            if any(alias.get(r) in ELEVATED_BY_DESIGN for r in receiver):
                continue
            # widen the window: a justification may sit above a multi-line
            # call, which is exactly where it belongs
            lo, hi = max(0, i - 12), min(len(lines), i + 3)
            context = "\n".join(lines[lo:hi])
            if "#" in context:
                continue
            R.fail(path, i + 1, "permissions",
                   "sudo() without a nearby justification comment")
    R.ok()


# ===================================================================== #
# Rule 7 -- manifest hygiene
# ===================================================================== #
MANIFEST_ORDER = ["name", "summary", "description", "version", "category",
                  "author", "maintainers", "license", "copyright",
                  "depends", "data", "assets",
                  "application", "installable"]


def rule_manifests():
    for path in sorted(glob.glob(os.path.join(ROOT, "mmux_*",
                                              "__manifest__.py"))):
        src = open(path, encoding="utf-8").read()
        try:
            data = ast.literal_eval(src)
        except Exception as exc:
            R.fail(path, 1, "odoo-guidelines", f"unparsable manifest: {exc}")
            continue
        for key in ("name", "version", "depends", "license"):
            if key not in data:
                R.fail(path, 1, "odoo-guidelines",
                       f"manifest missing required key '{key}'")
        # every module must carry the project's copyright attribution --
        # this is what makes the notice survive a new module (docs say it,
        # rule 14 checks the file headers, here the manifests are pinned)
        if data.get("author") != COPYRIGHT_HOLDER:
            R.fail(path, 1, "odoo-guidelines",
                   f"manifest author {data.get('author')!r} is not "
                   f"{COPYRIGHT_HOLDER!r}")
        if "Copyright" not in str(data.get("copyright", "")):
            R.fail(path, 1, "odoo-guidelines",
                   "manifest missing 'copyright' key")
        if not data.get("version", "").startswith("19.0."):
            R.fail(path, 1, "odoo-guidelines",
                   f"version {data.get('version')!r} is not an Odoo 19 version")
        # an empty data list is dead weight
        if data.get("data") == []:
            R.fail(path, 1, "occam",
                   "'data': [] adds nothing; omit the key instead")
        # declared order should follow the guideline
        keys = [k for k in data if k in MANIFEST_ORDER]
        ordered = sorted(keys, key=MANIFEST_ORDER.index)
        if keys != ordered:
            R.fail(path, 1, "odoo-guidelines",
                   f"manifest key order {keys} disagrees with the guideline")
    R.ok()


# ===================================================================== #
# Rule 8 -- no orphaned exports in the pure core
# ===================================================================== #
# Occam's razor, applied to one smell that has a definite answer: a name
# the pure core exports that nothing consumes.
#
# Getting this right needs three things the naive version missed, each of
# which produced false positives:
#
#   * **Internal use counts.** `language_chain` is called by `resolve_dict`
#     in its own module -- it is not dead just because no Odoo file names it.
#   * **Module-level instances count.** `CapabilityRegistry` is consumed as
#     the `CAPABILITY_REGISTRY` singleton, so the *class* name never
#     appears at a call site. Looking only for the class name flagged a
#     registry that is used everywhere.
#   * **Re-export is a use.** A name in `__init__.py`'s `__all__` is part
#     of the package's public surface on purpose.
#
# The check therefore asks "does this name appear anywhere outside its own
# definition, anywhere in the tree?" rather than "is it called by an Odoo
# file?".
def _snake_to_const(name):
    """`CapabilityRegistry` -> `CAPABILITY_REGISTRY` (the common singleton
    naming convention), so instance-style consumption is recognised."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()


def rule_pure_core_helpers_are_used():
    # every python file in the repo is fair game as a consumer
    all_py = py_files(*MODULE_DIRS)
    sources = {p: open(p, encoding="utf-8").read() for p in all_py}

    for path in py_files(PURE_CORE):
        own = sources[path]
        tree = ast.parse(own)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                name = node.name
            elif isinstance(node, ast.ClassDef):
                name = node.name
            else:
                continue
            if name.startswith("_"):
                continue  # explicitly private; not part of the surface

            candidates = (name, _snake_to_const(name))
            uses = 0
            for other_path, src in sources.items():
                stripped = src
                if other_path == path:
                    # strip the definition line itself, keep the rest of
                    # the module so internal callers are still counted
                    stripped = re.sub(
                        r"^(def|class)\s+%s\b" % re.escape(name),
                        "", src, count=1, flags=re.M)
                for cand in candidates:
                    uses += len(re.findall(r"\b%s\b" % re.escape(cand),
                                           stripped))
            if uses == 0:
                module_name = os.path.basename(path)[:-3]
                R.fail(path, node.lineno, "occam",
                       f"{module_name}.{name} is exported but never consumed")
    R.ok()


# ===================================================================== #
# Rule 9 -- module layout matches the Odoo convention
# ===================================================================== #
EXPECTED_SUBDIRS = {"models", "views", "security", "tests"}


def rule_module_layout():
    for module in MODULE_DIRS:
        base = os.path.join(ROOT, module)
        entries = set(os.listdir(base))
        for required in ("__init__.py", "__manifest__.py"):
            if required not in entries:
                R.fail(os.path.join(base, "__manifest__.py"), 1,
                       "odoo-guidelines", f"{module} missing {required}")
        if not (entries & EXPECTED_SUBDIRS):
            R.fail(os.path.join(base, "__manifest__.py"), 1,
                   "odoo-guidelines",
                   f"{module} has none of {sorted(EXPECTED_SUBDIRS)}")
    R.ok()


# ===================================================================== #
# Rule 10 -- every referenced module is actually imported
#
# Why this exists: `mmux.service` is split across files that all
# `_inherit` it.  Splitting a file moves method bodies but NOT the
# module-level imports, so a method can end up calling `json.loads()`
# in a file that never imported `json`.  That failure is invisible to
# AST comparison of method bodies (imports live outside them) and only
# surfaces at runtime, inside an `except` that swallows it -- which is
# precisely how it hid during the v3.1.1 split.
# ===================================================================== #
# Names worth guarding.  Deliberately a closed list: a NameError on any
# of these is always a real defect in this codebase.
#
# The first group is standard library; the second is the `odoo` layer
# this codebase actually uses.  `fields` is the reason the second group
# exists: `mmux_service_flow.py` called `fields.Datetime.now()` while
# importing only `api, models` from `odoo`, so every successful
# `task_report()` raised NameError.  The stdlib list could not see it --
# exactly the failure this rule was written for, one import layer up.
GUARDED_MODULES = (
    "json", "re", "base64", "hashlib", "datetime", "random",
    "itertools", "functools", "collections", "logging", "math",
    "uuid", "time", "os", "csv", "io",
    # odoo-layer names: same failure mode, same rule
    "fields", "tools", "http", "Command", "SUPERUSER_ID",
)


def rule_no_unimported_modules():
    for module in MODULE_DIRS:
        for path in py_files(module):
            src = open(path, encoding="utf-8").read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue                      # reported by the parse check

            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        imported.add(alias.asname or alias.name)

            # Only names used as the BASE of an attribute access count.
            # `json.loads(...)` is the missing-import signature; a bare
            # `fields` can equally be a parameter (`def __init__(self,
            # fields)` in schema.py) or a comprehension target, and
            # flagging those would be a false positive.  The rule exists
            # to catch "somebody calls X.y() in a file that never
            # imported X", so X.y() is exactly what it should match.
            used = set()
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)):
                    used.add(node.value.id)

            for mod in GUARDED_MODULES:
                if mod in used and mod not in imported:
                    R.fail(path, 0, "odoo-guidelines",
                           f"`{mod}.` is used but `import {mod}` is missing")
    R.ok()


# ===================================================================== #
# Rule 11 -- QWeb templates and every shipped XML file parse as XML
#
# Why this exists: `t-on-keydown="ev => ev.key === 'Enter' && go()"`
# looks harmless in an editor but is invalid XML -- `&` must be `&amp;`.
# Python's parser rejects the whole file, and so does the browser, so
# the widget silently never renders.  Nothing else in the toolchain
# catches it: the JS is fine, the Python is fine, and no unit test
# loads QWeb.  Cheap to check, expensive to miss.
# ===================================================================== #
def rule_xml_is_well_formed():
    import xml.dom.minidom
    for module in MODULE_DIRS:
        for path in xml_files(module):
            try:
                xml.dom.minidom.parse(path)
            except Exception as exc:
                R.fail(path, getattr(exc, "lineno", 0) or 0,
                       "odoo-guidelines",
                       f"XML does not parse: {exc}")
    R.ok()


# ===================================================================== #
# Rule 12 -- XML comments must not contain `--`
#
# Why this exists: rule 11 catches a template that does not parse, but a
# broken comment fails *identically* and so gets blamed on the markup
# around it.  The trap is real and easy to walk into: writing
# `here -- the intent is null` inside an XML comment is invalid XML,
# because a comment may not contain a double hyphen.  The error is
# reported at the comment's own line, so the check below names the exact
# problem instead of leaving "not well-formed" to be rediscovered.
# ===================================================================== #
_XML_COMMENT = re.compile(r"<!--([\s\S]*?)-->")


def rule_xml_comments_are_legal():
    for module in MODULE_DIRS:
        for path in xml_files(module):
            src = open(path, encoding="utf-8").read()
            for match in _XML_COMMENT.finditer(src):
                inner = match.group(1)
                # A trailing `-` is equally illegal (`--->`).
                if "--" in inner or inner.endswith("-"):
                    line = src[:match.start()].count("\n") + 1
                    R.fail(path, line, "odoo-guidelines",
                           "XML comment contains '--' (use a colon or a "
                           "single hyphen)")
    R.ok()


# ===================================================================== #
# Rule 13 -- every shipped JS file parses
#
# Why this exists: rule 11 taught the lesson for XML and this is the same
# lesson in the other medium.  A JS bundle is only assembled when Odoo
# serves assets, so a syntax error in a `.js` file is invisible to every
# other check here -- the Python is fine and the file is not read until a
# browser asks for it.  By then the symptom is "the button does nothing",
# with nothing in any log.
#
# Imports and exports are stripped before `node --check`, because the
# `@odoo-module` specifiers do not resolve outside the asset pipeline and
# resolving them is not the question -- *parsing* is.  Skipped silently
# when node is absent, so the linter stays usable without it.
# ===================================================================== #
_JS_IMPORT = re.compile(
    r'^\s*import\s+[\s\S]*?from\s+["\'][^"\']+["\'];\s*$', re.M)
_JS_EXPORT_LIST = re.compile(r"^\s*export\s*\{[^}]*\};\s*$", re.M)
_JS_EXPORT_DECL = re.compile(
    r"^\s*export\s+(class|const|function|let|var|default)", re.M)


def rule_js_is_well_formed():
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        R.ok()
        return
    for module in MODULE_DIRS:
        for path in glob.glob(os.path.join(ROOT, module, "static", "**",
                                           "*.js"), recursive=True):
            if "node_modules" in path:
                continue
            src = open(path, encoding="utf-8").read()
            body = _JS_IMPORT.sub("", src)
            body = _JS_EXPORT_LIST.sub("", body)
            body = _JS_EXPORT_DECL.sub(r"\1", body)
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".mjs", delete=False,
                        encoding="utf-8") as fh:
                    fh.write(body)
                    tmp = fh.name
                proc = subprocess.run([node, "--check", tmp],
                                      capture_output=True, text=True)
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
            if proc.returncode != 0:
                detail = [ln for ln in proc.stderr.strip().splitlines()
                          if ln.strip()]
                R.fail(path, 0, "odoo-guidelines",
                       "JS does not parse: " + (detail[0] if detail else ""))
    R.ok()


# ===================================================================== #
# Rule 14 -- copyright headers are present everywhere
#
# Why this exists: a copyright notice added once by hand decays the first
# time someone creates a new file.  The manifest `author`/`copyright` keys
# and the per-file header lines are all checked here, so "the project is
# Copyright jeffery9@gmail.com & geninit, LGPL-3" stays a pass/fail fact
# for every future file instead of an intention that silently rots.
# ===================================================================== #
PY_HEADER = [
    "# -*- coding: utf-8 -*-",
    f"# Copyright (C) 2026 {COPYRIGHT_HOLDER}",
    "# License LGPL-3.0 or later "
    "(https://www.gnu.org/licenses/lgpl-3.0.html)",
]


def rule_copyright_headers():
    paths = []
    for module in MODULE_DIRS + ["tests_pure", "tools"]:
        paths.extend(glob.glob(os.path.join(ROOT, module, "**", "*.py"),
                               recursive=True))
        paths.extend(glob.glob(os.path.join(ROOT, module, "static", "**",
                                            "*.js"), recursive=True))
    for path in sorted(set(paths)):
        if "node_modules" in path:
            continue
        lines = open(path, encoding="utf-8").read().splitlines()
        head = "\n".join(lines[:5])
        if os.path.basename(path) == "__manifest__.py":
            try:
                data = ast.literal_eval("\n".join(lines))
            except Exception:
                continue  # rule_manifests already reports unparsable ones
            if data.get("author") != COPYRIGHT_HOLDER:
                R.fail(path, 1, "copyright-header",
                       "manifest author must be "
                       f"{COPYRIGHT_HOLDER!r}, got {data.get('author')!r}")
            if "Copyright" not in str(data.get("copyright", "")):
                R.fail(path, 1, "copyright-header",
                       "manifest missing 'copyright' key")
            continue
        if path.endswith(".py"):
            if lines[:3] != PY_HEADER:
                R.fail(path, 1, "copyright-header",
                       "missing copyright header (expected the three "
                       "lines: coding, Copyright, License)")
        else:  # .js asset
            if f"Copyright (C) 2026 {COPYRIGHT_HOLDER}" not in head:
                R.fail(path, 1, "copyright-header",
                       "JS file missing copyright header block")
    R.ok()


RULES = [
    ("no raw SQL (active record)", rule_no_raw_sql),
    ("no print()", rule_no_print),
    ("pure core does not import odoo", rule_pure_core_is_pure),
    ("no DDD constructs", rule_no_ddd),
    ("models declare _description", rule_models_have_description),
    ("sudo() is justified", rule_sudo_is_justified),
    ("manifest hygiene", rule_manifests),
    ("pure-core helpers are used", rule_pure_core_helpers_are_used),
    ("module layout", rule_module_layout),
    ("no unimported module references", rule_no_unimported_modules),
    ("XML is well-formed", rule_xml_is_well_formed),
    ("XML comments are legal", rule_xml_comments_are_legal),
    ("JS is well-formed", rule_js_is_well_formed),
    ("copyright headers present", rule_copyright_headers),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    print("coding standards check  (docs/CODING_STANDARDS.md)")
    print("=" * 66)
    for label, fn in RULES:
        before = len(R.problems)
        try:
            fn()
            status = "ok" if len(R.problems) == before else "FAIL"
        except Exception as exc:
            R.fail(ROOT, 0, label, f"checker crashed: {type(exc).__name__}: {exc}")
            status = "ERROR"
        print(f"  [{status:5s}] {label}")

    print()
    if R.problems:
        print(f"{len(R.problems)} problem(s):")
        for path, line, rule, message in R.problems:
            loc = f"{path}:{line}" if line else path
            print(f"  {loc}: [{rule}] {message}")
    else:
        print("no problems found")

    print()
    print(f"{len(RULES)} rules checked")
    return 1 if R.problems else 0


if __name__ == "__main__":
    sys.exit(main())
