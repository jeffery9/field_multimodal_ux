# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""The agent loop: one reasoning step at a time, over a closed action set.

Architecture shift (v3.0, "pure agent")
---------------------------------------
Before this module the system was *LLM-first* but still **server-scripted**:
the LLM classified an utterance (`intent.py`), or picked the head of a
priority queue (`agent.py`), and then deterministic Python decided what to
do next. The control flow belonged to the code.

This module moves the *decision* to the model. The agent runs a ReAct loop:

    observe  -> the server hands over a bounded observation
    decide   -> the model emits ONE action
    validate -> the server accepts or rejects that action
    act      -> the action is carried out (never a business write)
    repeat   -> until finish / ask_user / budget exhausted

What does NOT move is authority. Three boundaries are structural, not
policy, and every one of them is enforced here rather than trusted:

1. **The action set is closed.** `AGENT_ACTIONS` is the complete list of
   things the agent can say. An action outside it is rejected, not
   interpreted. There is no "call this method" action and there never can
   be: `call_capability` names a *whitelisted capability id*, never a
   model or a method (the same rule `intent.py`/`plan.py` enforce).

2. **Payloads are closed per action.** `ACTION_PAYLOAD_KEYS` names the
   exact keys each action may carry. A payload with an undeclared key is
   rejected whole — we do not "use the good half". This is what stops a
   model from smuggling `model`/`method`/`ids` in beside a legitimate
   field, and it is why `FORBIDDEN_KEYS` is checked *again* even though
   the closed key set already excludes them (defence in depth: the
   blacklist is the shared口径 with the rest of the codebase).

3. **The agent cannot confirm.** `confirmed` is hardwired to False on
   every capability the loop proposes. An L2 capability therefore comes
   back `awaiting_confirmation` and the loop **stops there** — it does not
   retry with `confirmed=True`, does not treat the park as a failure, and
   does not "work around" it. The human confirmation gate of PRD
   FR-014/015/016 is the one thing the agent may never spend.

Everything this module can do is therefore either (a) *speak* (propose a
step, ask a question, answer, finish) or (b) *propose a whitelisted
capability for the server to run through the ExecutionGuard*. The agent is
the decision-maker, not the hand.

Why JSON-in-content rather than native tool calling
---------------------------------------------------
The provider contract (`provider.py:BaseProvider.call`) returns
`{"raw": str, "data": parsed}` and the OpenAI adapter hardcodes the
request body without a `tools` field. Rather than change the provider
surface and invalidate every existing path, the protocol is JSON inside
the message content — identical in shape to the intent / plan / pick
paths. This keeps one protocol for all four LLM surfaces and one
validation style for all four.

Multi-turn: `payload["messages"]` already carries a list, so the scratchpad
is rendered as alternating user/assistant turns. No provider change needed.

Purity
------
This module imports no Odoo and executes nothing. `run_loop` takes an
`LLMCaller` (any `(payload, config) -> dict`) and an optional
`CapabilityRunner` (a function the Odoo layer supplies, which is
`mmux.service.execute_capability`). Tests drive both with fakes.
"""
from typing import Any, Callable, Dict, List, Optional

from .i18n import resolve_text
from .intent import FORBIDDEN_KEYS, NORMALIZED_LANG

# ===================================================================== #
# the closed action set
# ===================================================================== #
#: Ask the server to run ONE whitelisted capability. `confirmed` is not
#: settable by the agent — see `step_from_action`.
ACT_CALL_CAPABILITY = "call_capability"
#: Narrate the next guided step to the user (no execution).
ACT_PROPOSE_STEP = "propose_step"
#: Stop and ask the human something (FR-005: ask, don't guess).
ACT_ASK_USER = "ask_user"
#: Answer a question in words only.
ACT_ANSWER = "answer"
#: The goal is met (or unreachable) — end the turn.
ACT_FINISH = "finish"
#: Hand the "which task?" decision to the queue-level agent (`agent.py`).
ACT_DELEGATE_PICK = "delegate_pick"

#: The COMPLETE list of actions the model may emit. Anything else is
#: rejected outright — this tuple is the boundary, not a suggestion.
AGENT_ACTIONS = (ACT_CALL_CAPABILITY, ACT_PROPOSE_STEP, ACT_ASK_USER,
                 ACT_ANSWER, ACT_FINISH, ACT_DELEGATE_PICK)

#: Which fields each action may carry. A payload key outside its action's
#: set rejects the whole action (see module docstring, boundary 2).
ACTION_PAYLOAD_KEYS = {
    ACT_CALL_CAPABILITY: frozenset({"capability_id", "params", "record_id",
                                    "why"}),
    ACT_PROPOSE_STEP: frozenset({"step_id", "title", "voice_prompt",
                                 "input_type", "capability_id", "params",
                                 "record_ref", "hint"}),
    ACT_ASK_USER: frozenset({"question", "options", "field", "why"}),
    ACT_ANSWER: frozenset({"text", "why"}),
    ACT_FINISH: frozenset({"summary", "goal_met", "why"}),
    ACT_DELEGATE_PICK: frozenset({"reason"}),
}

#: Free-text fields on any action, bounded so a chatty model cannot
#: inflate an audit row or a UI string without limit.
MAX_TEXT_LEN = 600
MAX_SUMMARY_LEN = 800
MAX_PARAMS_KEYS = 12
MAX_OPTIONS = 6

#: Provenance of one loop step. Mirrors `agent.py`'s tri-state so the
#: frontend can render "the model decided" vs "we fell back".
LOOP_SOURCE_LLM = "llm"
LOOP_SOURCE_RECIPE = "recipe_fallback"
LOOP_SOURCE_NONE = "no_provider"

#: Hard budgets. A loop that cannot end is a hang, so all are enforced
#: server-side (never by trusting the model to stop). These constants are
#: now DEFAULTS and fallbacks: the caller (Odoo service layer) may inject
#: per-deployment values from configuration via `run_loop`'s parameters;
#: when a configured value is missing or invalid the loop falls back here,
#: so the pure core never depends on configuration being present.
MAX_AGENT_TURNS = 6
#: At most this many capability *proposals* per loop. Proposing is cheap;
#: a loop that proposes six writes is almost certainly stuck, and stopping
#: is always safer than continuing.
MAX_AGENT_CAPABILITY_CALLS = 3
#: Repeats of the identical step (same action + same signature) that end
#: the loop. Guards the classic ReAct failure of proposing step A forever.
MAX_REPEATED_STEP = 2

#: Live keys of the scratchpad — the agent's whole working memory. Closed
#: by design: the model cannot widen its own memory, and nothing here is
#: secret (no other records, no other users' data). `confirmed` is
#: deliberately ABSENT: residual state must not be able to carry it.
SCRATCHPAD_KEYS = ("goal", "task_id", "flow_id", "skill_level", "lang",
                   "done_steps", "last_error", "user_answers", "turn",
                   "capability_calls")

#: Language keys accepted inside a multilingual text field. Mirrors the
#: languages `i18n.normalize_lang` can actually produce, so a payload is
#: never stripped of a language the rest of the system would resolve.
#: (A closed set is still important: without it the model could inject an
#: unbounded key space into the audit record and the UI.)
TEXT_LANG_KEYS = ("zh_CN", "zh", "en_US", "en", "ja_JP", "ja",
                  "ko_KR", "ko", "es_ES", "es", "fr_FR", "fr",
                  "de_DE", "de", "pt_PT", "pt", "default")

#: Loop lifecycle states surfaced to the caller.
LOOP_STATES = ("running", "awaiting_confirmation", "awaiting_user",
               "finished", "finished_all", "failed", "budget_exhausted")


def _reject_reason(code: str) -> Dict[str, str]:
    """A machine-readable rejection code (audit) — not user-facing text."""
    return code


def _as_count(value) -> int:
    """Coerce a persisted counter to a non-negative int, never raising.

    Counters arrive from a JSON blob that a previous version (or a human
    editing the log row) may have written as a string. A malformed value
    must degrade to 0 rather than break the loop — but it must NEVER
    degrade to a large number, which would silently exhaust the budget.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _clean_text(value, limit=MAX_TEXT_LEN):
    """Coerce a text field to a bounded string.

    Accepts a plain string OR a {lang: text} mapping (localization happens
    at the edge, exactly like `flow.py`). Anything else becomes "".
    Returns "" for empty so callers can treat falsy as "absent".
    """
    if isinstance(value, str):
        return value.strip()[:limit]
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if key not in TEXT_LANG_KEYS or not isinstance(val, str):
                continue
            text = val.strip()
            if text:
                out[key] = text[:limit]
        return out
    return ""


def _is_blank(value):
    """True when a cleaned text field carries nothing usable."""
    if isinstance(value, dict):
        return not value
    return not value


# ===================================================================== #
# the action object
# ===================================================================== #
class AgentAction:
    """ONE validated decision from the model.

    `ok=False` means the raw answer was rejected; `rejected_reason` then
    carries the machine-readable cause and `payload` is {} — the caller
    degrades to the recipe, never to a guess.
    """

    __slots__ = ("action", "payload", "ok", "rejected_reason", "raw_text")

    def __init__(self, action=None, payload=None, ok=False,
                 rejected_reason=None, raw_text=""):
        self.action = action
        self.payload = dict(payload or {})
        self.ok = bool(ok)
        self.rejected_reason = rejected_reason
        self.raw_text = raw_text

    # ---- typed accessors: callers never poke at payload directly ---- #
    @property
    def capability_id(self):
        return self.payload.get("capability_id")

    @property
    def params(self):
        return dict(self.payload.get("params") or {})

    @property
    def record_id(self):
        return self.payload.get("record_id")

    @property
    def question(self):
        return self.payload.get("question")

    @property
    def text(self):
        return self.payload.get("text") or self.payload.get("summary") or ""

    @property
    def is_terminal(self):
        """Actions that end the loop when they succeed."""
        return self.action in (ACT_FINISH,)

    @property
    def is_capability_call(self):
        return self.action == ACT_CALL_CAPABILITY

    @property
    def signature(self):
        """Stable identity of this step, for repeat detection.

        Two `propose_step` actions with the same step_id are "the same
        step" even if the wording drifted; two `call_capability` actions
        are the same when the capability, the target record AND the param
        keys match.

        `record_id=None` (which means *use the context record*) is spelled
        "ctx", never 0 — 0 is a legitimate record id in this vocabulary, so
        collapsing the two would let a real call on record 0 be silently
        suppressed as a "repeat".

        Only actions with a real *effect* get a repeat identity:
        `call_capability` (a write attempt) and `propose_step` (progress
        through a recipe). `answer` / `ask_user` are conversation, not
        progress — two answers in a row are a legitimate dialogue, and
        treating them as a spin would cut an operator off mid-explanation.
        They are identified by their text so distinct turns stay distinct.

        Params compare by key only — values are validated by the capability
        schema, and two different quantities on the same record are
        deliberately treated as the same step so a model cannot spin by
        re-proposing the same action with a tweaked number.
        """
        if self.action == ACT_CALL_CAPABILITY:
            return (self.action, self.capability_id,
                    self.record_id if self.record_id is not None else "ctx",
                    tuple(sorted(self.params)))
        if self.action == ACT_PROPOSE_STEP:
            return (self.action, self.payload.get("step_id"), None, ())
        if self.action == ACT_ASK_USER:
            return (self.action, _sig_text(self.payload.get("field")),
                    _sig_text(self.payload.get("question")), ())
        if self.action == ACT_ANSWER:
            # no side effect: identity is the text, so a repeated *idea*
            # is still detected but a normal back-and-forth is not.
            return (self.action, _sig_text(self.payload.get("text")),
                    None, ())
        return (self.action, None, None, ())

    def to_dict(self, lang=None):
        """Serialize for the trace / audit boundary (localizes texts)."""
        payload = dict(self.payload)
        for key in ("title", "voice_prompt", "hint", "question", "text",
                    "summary", "why"):
            if key in payload and isinstance(payload[key], dict):
                payload[key] = resolve_text(payload[key],
                                            lang or NORMALIZED_LANG)
        return {
            "action": self.action,
            "payload": payload,
            "ok": self.ok,
            "rejected_reason": self.rejected_reason,
            "signature": list(self.signature),
            "is_terminal": self.is_terminal,
            "is_capability_call": self.is_capability_call,
        }

    def __repr__(self):                                  # pragma: no cover
        return (f"<AgentAction {self.action!r} ok={self.ok} "
                f"rejected={self.rejected_reason!r}>")


# ===================================================================== #
# validation — the security boundary
# ===================================================================== #
def validate_action(data: Any,
                    allowed_capabilities=None) -> AgentAction:
    """Turn one raw model answer into an `AgentAction`, or reject it.

    `allowed_capabilities` is the server's authoritative whitelist for
    THIS loop (normally the enabled capability ids, optionally scoped to a
    scenario). It is the analogue of `agent.py`'s candidate set: the model
    may only name something that was actually offered.

    Rejection is total, always returns (never raises), and always records
    *why* — a rejected action is evidence, not noise.

    Order of checks:

    1. `data` is not a dict                -> ``not_a_mapping``
    2. `action` missing / not a string     -> ``missing_action``
    3. action outside `AGENT_ACTIONS`      -> ``unknown_action:<a>``
    4. payload not a mapping               -> ``bad_payload``
    5. payload key hits `FORBIDDEN_KEYS`   -> ``protocol_violation:<keys>``
    6. payload key outside the action's
       declared key set                     -> ``undeclared_payload:<keys>``
    7. per-action requirement check        -> ``<action>_requires_...``
    8. `call_capability` name not in the
       whitelist                            -> ``capability_not_whitelisted:<id>``

    Note the ordering of 5/6: the shared blacklist is checked first so a
    payload that tries to name a model is reported as a *protocol
    violation* (which describes intent) rather than merely as an
    undeclared key (which describes a typo).
    """
    allowed_caps = None if allowed_capabilities is None \
        else frozenset(allowed_capabilities)

    # (1) shape
    if not isinstance(data, dict):
        return AgentAction(ok=False, rejected_reason="not_a_mapping")

    # (2) the action itself
    action = data.get("action")
    if not isinstance(action, str) or not action:
        return AgentAction(ok=False, rejected_reason="missing_action")

    # (3) closed action set
    if action not in AGENT_ACTIONS:
        return AgentAction(ok=False,
                           rejected_reason=f"unknown_action:{action}")

    # (4) payload shape
    payload = data.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return AgentAction(action=action, ok=False,
                           rejected_reason="bad_payload")

    # (5) protocol violation — the shared blacklist, checked first
    violations = sorted(set(payload.keys()) & FORBIDDEN_KEYS)
    if violations:
        return AgentAction(action=action, ok=False,
                           rejected_reason="protocol_violation:"
                                           + ",".join(violations))

    # (6) closed payload keys for THIS action
    undeclared = sorted(set(payload.keys()) - ACTION_PAYLOAD_KEYS[action])
    if undeclared:
        return AgentAction(action=action, ok=False,
                           rejected_reason="undeclared_payload:"
                                           + ",".join(undeclared))

    clean = _clean_payload(action, payload)

    # (7) per-action requirements
    problem = _requirement_problem(action, clean, allowed_caps)
    if problem:
        return AgentAction(action=action, ok=False, rejected_reason=problem)

    return AgentAction(action=action, payload=clean, ok=True)


def _clean_payload(action, payload) -> Dict[str, Any]:
    """Normalize the declared fields of one action.

    Only declared keys survive (guaranteed by the caller), types are
    coerced and text fields are bounded. Params are passed through as a
    dict but capped in width — their *values* are validated by the
    capability's own schema in the ExecutionGuard, which is the only
    place that knows what they mean.
    """
    out: Dict[str, Any] = {}

    if action == ACT_CALL_CAPABILITY:
        out["capability_id"] = payload.get("capability_id")
        params = payload.get("params")
        clean_params = {}
        if isinstance(params, dict):
            for key in list(params)[:MAX_PARAMS_KEYS]:
                if isinstance(key, str):
                    clean_params[key] = params[key]
        out["params"] = clean_params
        rid = payload.get("record_id")
        # a record reference must be an int-ish id or an explicit context
        # marker; anything else is dropped rather than coerced.
        if isinstance(rid, bool):
            rid = None
        elif isinstance(rid, int):
            pass
        elif isinstance(rid, str) and rid.isdigit():
            rid = int(rid)
        elif isinstance(rid, str) and rid.startswith("$"):
            pass                      # context reference, resolved by Odoo
        else:
            rid = None
        out["record_id"] = rid
        out["why"] = _clean_text(payload.get("why"))

    elif action == ACT_PROPOSE_STEP:
        out["step_id"] = payload.get("step_id") \
            if isinstance(payload.get("step_id"), str) else None
        out["title"] = _clean_text(payload.get("title"))
        out["voice_prompt"] = _clean_text(payload.get("voice_prompt"))
        itype = payload.get("input_type")
        # the frontend renders by these only (see flow.py GuidedStep);
        # an unknown input_type would render nothing, so it is dropped.
        if itype not in ("scan", "voice_qty", "confirm", "capability"):
            itype = "confirm"
        out["input_type"] = itype
        cap_id = payload.get("capability_id")
        out["capability_id"] = cap_id if isinstance(cap_id, str) else None
        params = payload.get("params")
        clean_params = {}
        if isinstance(params, dict):
            for key in list(params)[:MAX_PARAMS_KEYS]:
                if isinstance(key, str):
                    clean_params[key] = params[key]
        out["params"] = clean_params
        out["record_ref"] = payload.get("record_ref")
        out["hint"] = _clean_text(payload.get("hint"))

    elif action == ACT_ASK_USER:
        out["question"] = _clean_text(payload.get("question"))
        opts = payload.get("options")
        clean_opts = []
        if isinstance(opts, list):
            for item in opts[:MAX_OPTIONS]:
                if isinstance(item, str) and item.strip():
                    clean_opts.append(item.strip()[:MAX_TEXT_LEN])
        out["options"] = clean_opts
        out["field"] = payload.get("field") \
            if isinstance(payload.get("field"), str) else None
        out["why"] = _clean_text(payload.get("why"))

    elif action == ACT_ANSWER:
        out["text"] = _clean_text(payload.get("text"))
        out["why"] = _clean_text(payload.get("why"))

    elif action == ACT_FINISH:
        out["summary"] = _clean_text(payload.get("summary"), MAX_SUMMARY_LEN)
        met = payload.get("goal_met")
        out["goal_met"] = bool(met) if isinstance(met, bool) else None
        out["why"] = _clean_text(payload.get("why"))

    elif action == ACT_DELEGATE_PICK:
        out["reason"] = _clean_text(payload.get("reason"))

    return out


def _requirement_problem(action, clean, allowed_caps) -> Optional[str]:
    """Per-action completeness / whitelist check. Returns a code or None."""
    if action == ACT_CALL_CAPABILITY:
        cap_id = clean.get("capability_id")
        if not isinstance(cap_id, str) or not cap_id:
            return "call_capability_requires_capability_id"
        # THE whitelist check: naming something we did not offer is a
        # hallucination (or an escalation attempt), never a fixable typo.
        if allowed_caps is not None and cap_id not in allowed_caps:
            return f"capability_not_whitelisted:{cap_id}"

    elif action == ACT_PROPOSE_STEP:
        # a step must at least say something to the operator
        if not clean.get("step_id"):
            return "propose_step_requires_step_id"
        if _is_blank(clean.get("title")) \
                and _is_blank(clean.get("voice_prompt")):
            return "propose_step_requires_text"

    elif action == ACT_ASK_USER:
        if _is_blank(clean.get("question")):
            return "ask_user_requires_question"

    elif action == ACT_ANSWER:
        if _is_blank(clean.get("text")):
            return "answer_requires_text"

    return None


# ===================================================================== #
# the observation the server hands the model
# ===================================================================== #
class AgentObservation:
    """What the agent is allowed to see at the top of a turn.

    Built by the server, never by the model. It is deliberately narrow:
    the goal, the current task/flow, the step the operator is on, the
    outcome of the previous action, and the answers given so far. It is
    NOT a database view — no other records, no other users' tasks, no
    customer data beyond the localized task title the operator can
    already see.
    """

    __slots__ = ("goal", "task", "flow_id", "skill_level", "lang",
                 "current_step", "steps", "last_result", "answers",
                 "available_capabilities")

    def __init__(self, goal="", task=None, flow_id=None, skill_level="novice",
                 lang=NORMALIZED_LANG, current_step=None, steps=None,
                 last_result=None, answers=None,
                 available_capabilities=None):
        self.goal = goal
        self.task = dict(task or {})
        self.flow_id = flow_id
        self.skill_level = skill_level
        self.lang = lang
        self.current_step = current_step
        self.steps = list(steps or [])
        self.last_result = last_result
        self.answers = dict(answers or {})
        self.available_capabilities = list(available_capabilities or ())

    def to_prompt_dict(self, lang=None):
        """The JSON the model actually receives.

        `available_capabilities` is the whitelist restated in the payload
        so the model can see exactly what it is permitted to name —
        offering it the list is not a leak, it is the whole constraint.
        """
        lg = lang or self.lang
        return {
            "goal": resolve_text(self.goal, lg) if self.goal else "",
            "task": {k: resolve_text(v, lg) if isinstance(v, dict) else v
                     for k, v in self.task.items()},
            "flow_id": self.flow_id,
            "skill_level": self.skill_level,
            "lang": lg,
            "current_step": (
                {k: resolve_text(v, lg) if isinstance(v, dict) else v
                 for k, v in self.current_step.items()}
                if isinstance(self.current_step, dict) else None),
            "steps_remaining": len(self.steps),
            "last_result": self.last_result,
            "answers": self.answers,
            "available_capabilities": self.available_capabilities,
        }


# ===================================================================== #
# the scratchpad — the agent's memory
# ===================================================================== #
class AgentScratchpad:
    """The agent's working memory, and nothing else.

    Persisted into `mmux.interaction.log.detail` by the Odoo layer, keyed
    by the loop's idempotency key, so a page reload resumes the same
    reasoning state. `to_dict` emits ONLY `SCRATCHPAD_KEYS`: the memory
    cannot be widened by anything the model says.

    There is intentionally no `confirmed` field. Confirmation is not a
    mental state the agent accumulates — it is a fresh human gesture
    required at the moment of each L2 write.

    `__slots__` is `SCRATCHPAD_KEYS` plus one runtime-only slot. The
    persisted / emitted key set stays exactly `SCRATCHPAD_KEYS` (see
    `to_dict`), so memory cannot be widened by anything the model says.
    """

    __slots__ = SCRATCHPAD_KEYS + ("_repeats", "_max_turns", "_max_calls")

    def __init__(self, goal="", task_id=None, flow_id=None,
                 skill_level="novice", lang=NORMALIZED_LANG,
                 done_steps=None, last_error=None, user_answers=None,
                 turn=0, capability_calls=0,
                 max_turns=MAX_AGENT_TURNS,
                 max_capability_calls=MAX_AGENT_CAPABILITY_CALLS):
        self.goal = goal
        self.task_id = task_id
        self.flow_id = flow_id
        self.skill_level = skill_level
        self.lang = lang
        self.done_steps = list(done_steps or [])
        self.last_error = last_error
        self.user_answers = dict(user_answers or {})
        # counters come from a persisted JSON blob, so they must survive
        # being handed a string / None / a float rather than raising.
        self.turn = _as_count(turn)
        self.capability_calls = _as_count(capability_calls)
        # runtime-only repeat ledger; deliberately not in the persisted
        # shape (see note_repeat).
        self._repeats: Dict[str, int] = {}
        # runtime-only budgets, same class as `_repeats`: they are injected
        # by `run_loop` on every step (from caller configuration), never
        # persisted — a resumed loop gets them re-injected fresh, so a
        # mid-session budget tightening takes effect immediately (safe
        # direction: an over-budget loop terminates rather than running on).
        # `<=0` falls back to the module default: an empty/misconfigured
        # value must never mean "unlimited".
        self._max_turns = _as_count(max_turns) or MAX_AGENT_TURNS
        self._max_calls = (_as_count(max_capability_calls)
                           or MAX_AGENT_CAPABILITY_CALLS)

    # ---- mutation helpers (kept tiny and explicit) ----------------- #
    def mark_step(self, step_id):
        """Record a completed step exactly once (order preserved)."""
        if step_id and step_id not in self.done_steps:
            self.done_steps.append(step_id)

    def answer(self, key, value):
        if isinstance(key, str) and key:
            self.user_answers[key] = value

    def note_error(self, message):
        self.last_error = str(message)[:MAX_TEXT_LEN] if message else None

    def note_turn(self):
        self.turn += 1

    def note_capability_call(self):
        self.capability_calls += 1

    # ---- repeat ledger --------------------------------------------- #
    #: A per-step occurrence counter, *not* part of `SCRATCHPAD_KEYS`.
    #: It is bookkeeping for spin detection, not agent memory: deriving it
    #: from `done_steps` would be wrong (a legitimate re-proposal after a
    #: failure must be allowed) and persisting it would let a resumed
    #: loop's history be rewritten by whoever holds the log row.
    def note_repeat(self, sig):
        self._repeats[sig] = self._repeats.get(sig, 0) + 1

    def repeats(self, sig):
        return self._repeats.get(sig, 0)

    @property
    def budget_left(self):
        return max(0, self._max_turns - self.turn)

    @property
    def calls_left(self):
        return max(0, self._max_calls - self.capability_calls)

    def to_dict(self):
        """Only the declared keys — memory is not extensible at runtime."""
        return {key: getattr(self, key) for key in SCRATCHPAD_KEYS}

    @classmethod
    def from_dict(cls, data):
        """Restore from a persisted detail blob; unknown keys are dropped
        and malformed values fall back to defaults (never raises)."""
        if not isinstance(data, dict):
            return cls()
        kwargs = {}
        for key in SCRATCHPAD_KEYS:
            if key in data:
                kwargs[key] = data[key]
        try:
            return cls(**kwargs)
        except (TypeError, ValueError):                   # defensive
            return cls()

    def __repr__(self):                                  # pragma: no cover
        return (f"<AgentScratchpad turn={self.turn} "
                f"calls={self.capability_calls} done={len(self.done_steps)}>")


# ===================================================================== #
# one turn's outcome
# ===================================================================== #
class AgentTurn:
    """Everything the caller needs to render and audit ONE loop step."""

    __slots__ = ("action", "state", "source", "observation", "exec_result",
                 "speak", "trace_entry", "rejected_reason")

    def __init__(self, action=None, state="running", source=LOOP_SOURCE_LLM,
                 observation=None, exec_result=None, speak="",
                 rejected_reason=None):
        self.action = action
        self.state = state
        self.source = source
        self.observation = observation
        self.exec_result = exec_result
        self.speak = speak                # text for TTS / display
        self.rejected_reason = rejected_reason
        self.trace_entry = (action.to_dict(None) if action is not None else None)

    @property
    def is_terminal(self):
        return self.state in ("finished", "finished_all", "failed",
                              "budget_exhausted")

    def to_dict(self, lang=None):
        out = {
            "state": self.state,
            "source": self.source,
            "action": self.action.to_dict(lang) if self.action is not None else None,
            "speak": resolve_text(self.speak, lang) if self.speak else "",
            "rejected_reason": self.rejected_reason,
            "loop_state": self.state,
        }
        if self.exec_result is not None:
            out["exec"] = self.exec_result
        if self.observation is not None:
            out["observation"] = self.observation.to_prompt_dict(lang)
        return out

    def __repr__(self):                                  # pragma: no cover
        return (f"<AgentTurn state={self.state!r} source={self.source!r} "
                f"action={getattr(self.action, 'action', None)!r}>")


# ===================================================================== #
# prompt construction
# ===================================================================== #
def _action_contract() -> Dict[str, Any]:
    """The machine-readable contract handed to the model.

    Generated from the same tuples the validator uses, so the prompt can
    never drift out of sync with what is actually accepted. If an action
    is removed from `AGENT_ACTIONS` it disappears from the prompt in the
    same edit.
    """
    return {
        action: sorted(ACTION_PAYLOAD_KEYS[action])
        for action in AGENT_ACTIONS
    }


def build_loop_prompt(scratchpad: AgentScratchpad,
                      observation: AgentObservation,
                      history: Optional[List[Dict[str, Any]]] = None,
                      lang=None) -> List[Dict[str, str]]:
    """Render the multi-turn message list for one loop step.

    Structure:
      system  — who you are, the closed contract, the three hard rules
      user    — the observation (server-built, JSON)
      ...     — prior assistant action / user observation pairs
      user    — the current observation again (what to decide on NOW)

    The rules stated in prose are backstops for good behaviour; the
    enforcement lives in `validate_action` and `run_loop`. A model that
    ignores every word of the prompt still cannot exceed its authority.
    """
    lg = lang or scratchpad.lang or NORMALIZED_LANG
    sys = (
        "You are the operations agent embedded in an Odoo field assistant. "
        "You guide ONE human operator through ONE task, one step at a time.\n"
        "\n"
        "You decide what happens next. Choose exactly ONE action per turn "
        "and reply with ONE JSON object:\n"
        '  {"action": "<one of the action names>", "payload": {...}}\n'
        "\n"
        "The COMPLETE set of actions and the ONLY payload keys each may "
        "carry (generated from the server's own validator — anything else "
        "is rejected):\n" + json_dumps(_action_contract()) + "\n"
        "\n"
        "Hard rules, in order of importance:\n"
        "1. You may ONLY name a capability_id that appears in "
        "`available_capabilities` below. Never invent an id. You may NEVER "
        "name an Odoo model, field or method — there is no action for it "
        "and such a payload is rejected.\n"
        "2. You can PROPOSE but never CONFIRM. Business actions that need "
        "human confirmation come back as `awaiting_confirmation` and the "
        "loop stops there. Do not try to get around this; there is no "
        "field for it and no other action that does the write.\n"
        "3. Never guess business-critical values (quantities, lots, money, "
        "destinations). If you need one, use `ask_user` and stop — that is "
        "the correct, expected behaviour, not a failure.\n"
        "4. Prefer `propose_step` while the operator is working (it is "
        "what they hear), and `call_capability` only when the step is "
        "actually done. Use `finish` when the goal is met.\n"
        "\n"
        "Write all user-facing text in the operator's language "
        f"(lang = {lg}). Always reply with JSON. Never add prose outside "
        "the JSON object."
    )
    messages: List[Dict[str, str]] = [{"role": "system", "content": sys}]
    # prior turns, so the model sees what it already did and what happened
    # NOTE: this is the PROMPT WINDOW SIZE, not the behaviour budget — it
    # deliberately stays the module constant even though run_loop accepts
    # configured budgets. Trimming history is about token cost, not about
    # how many turns the loop may take.
    for entry in (history or [])[-MAX_AGENT_TURNS:]:
        role = entry.get("role")
        content = entry.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            messages.append({"role": role, "content": content})
    # the current observation is always the last user turn
    messages.append({
        "role": "user",
        "content": json_dumps({
            "scratchpad": scratchpad.to_dict(),
            "budget": {"turns_left": scratchpad.budget_left,
                       "capability_calls_left": scratchpad.calls_left},
            "observation": observation.to_prompt_dict(lg),
        }),
    })
    return messages


def json_dumps(value):
    """Deterministic JSON for prompts (imported lazily to keep this module
    importable in constrained environments)."""
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


# ===================================================================== #
# action -> guided step / execution request
# ===================================================================== #
def step_from_action(action: AgentAction, skill_level="novice"):
    """Turn a `propose_step` action into a `GuidedStep`-shaped dict.

    Returned as a dict rather than a `GuidedStep` so this module stays
    free of `flow.py`'s constructor rules; the Odoo layer feeds it to
    `TaskFlowEngine.cursor_view` / the frontend, which render the same
    shape either way (step_id / title / voice_prompt / input_type / ...).

    `input_type` is already normalized to the closed set by
    `_clean_payload`, so the frontend can always render it.
    """
    if action is None or action.action != ACT_PROPOSE_STEP:
        return None
    payload = action.payload
    return {
        "step_id": payload.get("step_id"),
        "title": payload.get("title"),
        "voice_prompt": payload.get("voice_prompt"),
        "input_type": payload.get("input_type") or "confirm",
        "capability_id": payload.get("capability_id"),
        "params": dict(payload.get("params") or {}),
        "record_ref": payload.get("record_ref"),
        "hint": payload.get("hint"),
        "skill": skill_level,
        "agent_generated": True,
    }


def execution_request(action: AgentAction):
    """Turn a `call_capability` action into the ARGUMENTS of
    `mmux.service.execute_capability`, with `confirmed` hardwired False.

    This function is the single place where an agent-proposed write turns
    into an execution call, and it is deliberately the *only* such place:
    `confirmed=False` is written here, not passed in, so there is no code
    path by which a model's answer can arrive at the guard pre-confirmed.
    An L2 capability will therefore always come back
    `awaiting_confirmation`, and the loop stops there.
    """
    if action is None or action.action != ACT_CALL_CAPABILITY:
        return None
    return {
        "capability_id": action.capability_id,
        "params": action.params,
        "record_id": action.record_id,
        "confirmed": False,           # NEVER from the model
    }


# ===================================================================== #
# the loop
# ===================================================================== #
def _fallback_action(observation, scratchpad) -> AgentAction:
    """The deterministic step used when the model is unavailable or wrong.

    This is what keeps the "pure agent" architecture from being a
    single point of failure: if the LLM cannot be reached, the operator
    still gets exactly the guided recipe they had before v3.0 — narrated
    in order, with no made-up content and no execution.
    """
    current = observation.current_step or {}
    step_id = current.get("step_id")
    if step_id and step_id not in scratchpad.done_steps:
        return AgentAction(action=ACT_PROPOSE_STEP, ok=True, payload={
            "step_id": step_id,
            "title": current.get("title"),
            "voice_prompt": current.get("voice_prompt"),
            "input_type": current.get("input_type") or "confirm",
            "capability_id": current.get("capability_id"),
            "params": dict(current.get("params") or {}),
            "record_ref": current.get("record_ref"),
            "hint": current.get("hint"),
            "why": "recipe",
        })
    if current:
        # the recipe has nothing left to narrate
        return AgentAction(action=ACT_FINISH, ok=True,
                           payload={"goal_met": True, "why": "recipe"})
    # no current step and nothing to do -> hand over to the task agent
    return AgentAction(action=ACT_DELEGATE_PICK, ok=True)


def run_loop(scratchpad: AgentScratchpad,
             observation: AgentObservation,
             llm_call: Optional[Callable] = None,
             llm_config: Optional[Dict[str, Any]] = None,
             run_capability: Optional[Callable] = None,
             allowed_capabilities=None,
             history: Optional[List[Dict[str, Any]]] = None,
             max_turns: int = MAX_AGENT_TURNS,
             max_capability_calls: int = MAX_AGENT_CAPABILITY_CALLS,
             max_repeated_step: int = MAX_REPEATED_STEP) -> AgentTurn:
    """Advance the loop by exactly ONE step and report what happened.

    One step at a time, deliberately. A single call that ran until
    `finish` would hold a transaction open across several LLM round-trips
    and several writes, and would make `awaiting_confirmation` impossible
    to surface (there would be no moment to ask). The caller (Odoo layer /
    frontend) drives the cadence; this function owns the logic.

    `llm_call(payload, config) -> {"data": <parsed>}` is any provider call
    (normally `PROVIDER_REGISTRY.call` partially applied). `run_capability`
    is normally `mmux.service.execute_capability`; when absent, a
    capability call is *proposed* but not run, and the state says so.

    Returns an `AgentTurn`. Never raises for a model problem.
    """
    turn = AgentTurn(observation=observation)

    # ---- budget: enforced server-side, before anything is asked ----- #
    # Inject the caller's budgets into the scratchpad FIRST so that the
    # prompt (via `budget_left`/`calls_left`) and the checks below read
    # the same numbers the caller configured — one source, no drift.
    # Defaults keep the legacy constant behaviour when nothing is passed.
    scratchpad._max_turns = _as_count(max_turns) or MAX_AGENT_TURNS
    scratchpad._max_calls = (_as_count(max_capability_calls)
                             or MAX_AGENT_CAPABILITY_CALLS)
    # same clamp for the repeat limit: None/0/negative must mean the
    # module default, never "no limit" and never a comparison crash.
    max_repeated_step = (_as_count(max_repeated_step)
                         or MAX_REPEATED_STEP)
    if scratchpad.turn >= scratchpad._max_turns:
        turn.state = "budget_exhausted"
        turn.action = AgentAction(action=ACT_FINISH, ok=True,
                                  payload={"goal_met": None,
                                           "why": "turn budget exhausted"})
        turn.rejected_reason = "budget_exhausted"
        return turn

    # ---- 1. decide: ask the model, or fall back --------------------- #
    action = None
    source = LOOP_SOURCE_LLM
    raw_action = None

    if llm_call is None:
        source = LOOP_SOURCE_NONE
    else:
        prompt = build_loop_prompt(scratchpad, observation, history)
        try:
            raw = llm_call({"messages": prompt}, llm_config or {})
            raw_action = _extract_action(raw)
        except Exception:                                # provider errors
            raw_action = None
        if raw_action is None:
            # no provider / timeout / unparseable — degrade, never fail
            source = LOOP_SOURCE_RECIPE if llm_call is not None \
                else LOOP_SOURCE_NONE
        else:
            candidate = validate_action(raw_action, allowed_capabilities)
            if candidate.ok:
                action = candidate
            else:
                # a rejected answer must not silently become a restart of
                # the model: take the deterministic recipe instead, and
                # keep the reason for the audit trail.
                turn.rejected_reason = candidate.rejected_reason
                source = LOOP_SOURCE_RECIPE

    if action is None:
        action = _fallback_action(observation, scratchpad)

    turn.action = action
    turn.source = source

    # ---- 2. repeat detection: a stuck agent must not spin ----------- #
    # Count this *upcoming* occurrence, not just the completed ones: the
    # check runs before the action is recorded, so comparing against
    # `done_steps.count(...)` alone would let the loop exceed the limit by
    # one every time and never actually trip.
    if not action.is_terminal:
        sig = _sig_key(action)
        occurrences = scratchpad.repeats(sig) + 1
        if occurrences > max_repeated_step:
            turn.state = "budget_exhausted"
            turn.rejected_reason = turn.rejected_reason or "repeated_step"
            turn.action = AgentAction(action=ACT_FINISH, ok=True,
                                      payload={"goal_met": None,
                                               "why": "the same step was "
                                                      "proposed repeatedly"})
            return turn
        scratchpad.note_repeat(sig)

    # ---- 3. act ------------------------------------------------------ #
    turn.state = "running"
    scratchpad.note_turn()

    if action.action == ACT_FINISH:
        met = action.payload.get("goal_met")
        turn.state = "finished" if met is not False else "failed"
        turn.speak = action.payload.get("summary") or ""
        return turn

    if action.action == ACT_ASK_USER:
        turn.state = "awaiting_user"
        turn.speak = action.payload.get("question") or ""
        return turn

    if action.action == ACT_ANSWER:
        turn.state = "running"
        turn.speak = action.payload.get("text") or ""
        return turn

    if action.action == ACT_DELEGATE_PICK:
        # the "which task next?" decision belongs to `agent.py`; this loop
        # does not duplicate it, it hands over.
        turn.state = "finished_all"
        turn.speak = action.payload.get("reason") or ""
        return turn

    if action.action == ACT_PROPOSE_STEP:
        step_id = action.payload.get("step_id")
        scratchpad.mark_step(_sig_key(action))
        if step_id:
            scratchpad.mark_step(step_id)
        turn.state = "running"
        turn.speak = action.payload.get("voice_prompt") \
            or action.payload.get("title") or ""
        return turn

    if action.action == ACT_CALL_CAPABILITY:
        # budget the *proposals*, so a stuck loop cannot keep writing
        if scratchpad.capability_calls >= scratchpad._max_calls:
            turn.state = "budget_exhausted"
            turn.rejected_reason = "capability_call_budget_exhausted"
            turn.action = AgentAction(action=ACT_FINISH, ok=True,
                                      payload={"goal_met": None,
                                               "why": "too many capability "
                                                      "proposals"})
            return turn
        scratchpad.note_capability_call()
        scratchpad.mark_step(_sig_key(action))

        request = execution_request(action)
        if run_capability is None:
            # no runner wired: report the proposal without executing. The
            # agent has proposed; the server (and a human) decide.
            turn.state = "awaiting_confirmation"
            turn.rejected_reason = "no_runner"
            turn.exec_result = {"status": "proposed",
                                "capability_id": request["capability_id"]}
            return turn

        try:
            result = run_capability(request)
        except Exception as exc:                         # never crash a loop
            scratchpad.note_error(exc)
            turn.state = "failed"
            turn.exec_result = {"status": "failed", "error": str(exc)}
            return turn

        turn.exec_result = result
        status = (result or {}).get("status")
        if status == "success":
            turn.state = "running"
        elif status == "awaiting_confirmation":
            # THE gate. Park the loop; the human decides. The agent does
            # not retry, does not escalate, does not try another route.
            # `speak` is left empty: the confirmation prompt comes from
            # the capability's own `confirmation` payload, not from here.
            turn.state = "awaiting_confirmation"
        else:
            # failed / unknown / duplicate: record and let the caller
            # decide whether to continue. `unknown` must never be retried
            # blindly (PRD FR-017) — the loop simply stops reporting
            # success.
            scratchpad.note_error(status or "unknown")
            turn.state = "failed"
        return turn

    # unreachable: AGENT_ACTIONS is closed and every member is handled
    turn.state = "failed"
    turn.rejected_reason = f"unhandled_action:{action.action}"
    return turn


def _sig_key(action) -> str:
    """Signature as a hashable string, for scratchpad bookkeeping."""
    return "|".join("" if part is None else str(part)
                    for part in action.signature)


def _sig_text(value):
    """Flatten a possibly-multilingual text field for signature purposes.

    Uses the raw mapping's sorted items when it is a dict, so the same
    sentence in any language order yields the same identity.
    """
    if isinstance(value, dict):
        return "|".join(f"{k}={v}" for k, v in sorted(value.items()))
    return value


def _extract_action(raw):
    """Pull the action object out of a provider answer.

    Mirrors the tolerant-but-strict reading used by `parse_intent` /
    `_agent_pick`: accept `{"data": {...}}`, a JSON string, or a raw JSON
    string in `raw`. Returns None when nothing usable was produced —
    which is a *degradation*, not an error.
    """
    if not isinstance(raw, dict):
        return None
    data = raw.get("data")
    if data is None and isinstance(raw.get("raw"), str):
        text = raw["raw"]
        data = _try_json(text)
    if isinstance(data, str):
        data = _try_json(data)
    if isinstance(data, dict) and "action" in data:
        return data
    # tolerate an action nested one level down ({"data": {"action": ...}})
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, dict) and "action" in value:
                return value
    return None


def _try_json(text):
    import json
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
