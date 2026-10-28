# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Agent task selection: "which of my tasks should I do NOW?".

The task queue (`taskqueue.py`) answers the deterministic question
*"given these candidates, which one sorts first?"*. That is a scheduler,
not an agent: it cannot weigh an overdue high-value customer against a
merely older document, and it cannot explain itself.

This module adds the missing decision layer. It is the *fourth* member of
the LLM-output-validation family, alongside:

    intent.py   LLM text     -> capability_id + params
    plan.py     LLM text     -> ordered list of capability steps
    flow.py     LLM text     -> a guided navigation step
    agent.py    LLM decision -> ONE task_id taken from the server's set

Security model (this is the whole point of the module)
------------------------------------------------------
The LLM is given a **server-authoritative, already-trimmed candidate
list** and may return exactly one `selected_task_id`. That id MUST be a
member of the candidate set built for *this* request, from records the
*current user* is allowed to see (the Odoo layer searches under the
user's own identity — never `sudo`). Anything else is a hallucination or
an escalation attempt and is rejected outright; we never "fix up" an id,
never fuzzy-match a title, never fall back to the first candidate.

Two invariants worth stating out loud, because they are the boundaries
most likely to be quietly "optimised away" later:

1. **The authoritative source of `selected_task_id` is always the
   server-side candidate set of this request.** It is never the LLM text
   and never a value round-tripped from the client. If someone ever makes
   the selected id a parameter the frontend passes straight into the
   service layer, membership validation becomes decorative and the agent
   can be told to "select" any record in the database.

2. **`alternatives[].task_id` must be validated too.** It looks like a
   display-only field, which is exactly why it gets forgotten — but an
   unvalidated alternative lets the model make the UI render identifiers
   of records the user has no right to see. Out-of-set alternatives are
   dropped individually rather than failing the whole pick.

The module is pure: it imports no Odoo and executes nothing. Selecting a
task grants **zero authority** — completion still travels the normal
`task_report -> execute_capability -> ExecutionGuard` funnel, so an L2
write is still parked for explicit human confirmation (PRD FR-014/015/016
unchanged).

Degradation is always available: if the model is unreachable, returns
garbage, or violates the protocol, `fallback_pick()` reproduces the
original deterministic queue ordering *over the very same candidate set*,
so the operator always gets a task.
"""
from typing import Any, Dict, List, Optional

from .i18n import resolve_text
from .intent import FORBIDDEN_KEYS, NORMALIZED_LANG
from .taskqueue import TASK_ENGINE, TaskQueueEngine

# ===================================================================== #
# decision provenance
# ===================================================================== #
#: the model made the call (validated against the candidate set)
SOURCE_LLM = "llm"
#: the model was unavailable / invalid -> deterministic queue ordering
SOURCE_FALLBACK = "queue_fallback"
#: there was nothing to choose from in the first place
SOURCE_EMPTY = "no_candidates"

#: at most this many rejected alternatives are surfaced to the user
MAX_ALTERNATIVES = 3

#: only these language keys survive in a `reason` mapping. An arbitrary
#: key set would let the model inject junk into the audit record and the
#: UI, so the shape is closed to the languages we actually resolve.
REASON_LANG_KEYS = ("zh_CN", "zh", "en_US", "en", "default")

#: hard bounds for the free-text fields, so a chatty model cannot inflate
#: an audit row or a UI string without limit.
MAX_REASON_LEN = 400
MAX_WHY_NOT_LEN = 200


class AgentPick:
    """The outcome of one "which task now?" decision.

    `task_id is None` means "no usable decision" — the caller must then
    consult `source` / `rejected_reason` to learn why:

    * ``SOURCE_EMPTY``    — the candidate set was empty to begin with.
    * ``SOURCE_FALLBACK`` — the model failed; the pick came from the queue.
    """

    __slots__ = ("task_id", "source", "reason", "confidence",
                 "alternatives", "rejected_reason", "raw_text")

    def __init__(self, task_id=None, source=SOURCE_FALLBACK, reason=None,
                 confidence=0.0, alternatives=None, rejected_reason=None,
                 raw_text=""):
        self.task_id = task_id
        self.source = source
        # keep the ORIGINAL multilingual mapping; localisation happens in
        # to_dict() so the audit record stays evidence-complete.
        self.reason = dict(reason or {})
        self.confidence = confidence
        self.alternatives = list(alternatives or [])
        self.rejected_reason = rejected_reason
        self.raw_text = raw_text

    @property
    def is_valid(self):
        """True when we have an actual task to hand to the operator."""
        return bool(self.task_id)

    @property
    def is_autonomous(self):
        """True only when the LLM made a *validated* choice."""
        return self.is_valid and self.source == SOURCE_LLM

    def to_dict(self, lang=None):
        """Serialize for the frontend / audit boundary."""
        return {
            "task_id": self.task_id,
            "source": self.source,
            "reason": resolve_text(self.reason, lang or NORMALIZED_LANG),
            "reason_i18n": dict(self.reason),
            "confidence": round(float(self.confidence or 0.0), 3),
            "alternatives": [
                {
                    "task_id": alt.get("task_id"),
                    "why_not": resolve_text(alt.get("why_not"),
                                            lang or NORMALIZED_LANG),
                }
                for alt in self.alternatives
            ],
            "rejected_reason": self.rejected_reason,
        }

    def __repr__(self):                                  # pragma: no cover
        return (f"<AgentPick {self.task_id!r} source={self.source!r} "
                f"confidence={self.confidence:.2f}>")


# ===================================================================== #
# helpers
# ===================================================================== #
def _clamp(value) -> float:
    """Coerce to a 0..1 float; garbage becomes 0.0 (never raises)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


def _clean_reason(reason) -> Dict[str, str]:
    """Keep only whitelisted language keys, each a bounded string.

    A `reason` must not be able to smuggle arbitrary structure into the
    audit record or the UI, so unknown keys are stripped rather than
    trusted. A plain string is accepted as the default-language text.
    """
    if isinstance(reason, str):
        return {"default": reason[:MAX_REASON_LEN]} if reason else {}
    if not isinstance(reason, dict):
        return {}
    out: Dict[str, str] = {}
    for key, val in reason.items():
        if key not in REASON_LANG_KEYS:
            continue
        if not isinstance(val, str):
            continue
        text = val.strip()
        if text:
            out[key] = text[:MAX_REASON_LEN]
    return out


def _clean_alternatives(raw, allowed_ids, exclude_id=None) -> List[dict]:
    """Validate `alternatives` one by one against the candidate set.

    An entry whose `task_id` is not in `allowed_ids` is DROPPED, not
    treated as a fatal error: one stray id should not cost the operator
    the (perfectly valid) primary selection. The result is capped at
    `MAX_ALTERNATIVES`.
    """
    if not isinstance(raw, list):
        return []
    out: List[dict] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        tid = item.get("task_id")
        if not isinstance(tid, str) or tid not in allowed_ids:
            continue
        if tid == exclude_id or tid in seen:
            continue
        seen.add(tid)
        why = item.get("why_not")
        if isinstance(why, dict):
            why = _clean_reason(why)          # localise later via to_dict
        elif isinstance(why, str):
            why = why.strip()[:MAX_WHY_NOT_LEN]
        else:
            why = ""
        out.append({"task_id": tid, "why_not": why})
        if len(out) >= MAX_ALTERNATIVES:
            break
    return out


def _candidate_ids(candidates, only_source=None) -> List[str]:
    """The authoritative set of ids the model is allowed to name."""
    pool = [t for t in (candidates or [])
            if not only_source or t.source_id == only_source]
    return [t.task_id for t in pool]


def _fallback_reason() -> Dict[str, str]:
    """Explain, in the user's language, why the queue decided for them.

    Surfaced verbatim by the UI, so the operator is never left guessing
    whether a human-like decision was made or a scheduler ran.
    """
    return {
        "zh_CN": "智能体暂不可用，已按优先级队列取下一个任务。",
        "zh": "智能体暂不可用，已按优先级队列取下一个任务。",
        "en_US": "Agent unavailable; took the next task by queue priority.",
        "en": "Agent unavailable; took the next task by queue priority.",
    }


# ===================================================================== #
# validation (the security anchor)
# ===================================================================== #
def validate_pick(data: Any, candidates: List, normalized_lang=NORMALIZED_LANG,
                  only_source: str = None) -> AgentPick:
    """Turn raw LLM JSON into an `AgentPick`, or reject it.

    Rejection is total and silent-by-design: we return an invalid pick
    (with `rejected_reason` set for the audit trail) and let the caller
    degrade to the deterministic queue. We never guess, never repair, and
    never raise — a malformed model answer must not break the loop.

    Rejection conditions, in order:

    1. `data` is not a dict;
    2. a top-level key hits `FORBIDDEN_KEYS` (naming a model/method is a
       protocol violation — the *same* frozenset `intent.py` uses);
    3. `selected_task_id` is missing or not a string;
    4. `selected_task_id` is not in this request's candidate set
       (hallucination or privilege escalation) -- the core invariant;
    5. the candidate set is empty (nothing to have chosen from).

    `alternatives` are filtered entry-by-entry (see `_clean_alternatives`)
    and never invalidate an otherwise-valid primary choice. `confidence`
    is clamped and `reason` is key-whitelisted; neither can reject a pick.
    """
    allowed = _candidate_ids(candidates, only_source)

    def _reject(reason_code, raw=""):
        return AgentPick(source=SOURCE_FALLBACK, rejected_reason=reason_code,
                         raw_text=raw)

    # (5) nothing to choose from -- an empty candidate set is not a
    #     model failure, it just means the operator is all caught up.
    if not allowed:
        return AgentPick(source=SOURCE_EMPTY, rejected_reason="no_candidates")

    # (1) shape
    if not isinstance(data, dict):
        return _reject("not_a_mapping")

    # (2) protocol violation -- reuse the shared blacklist so the口径 is
    #     identical to the intent path.
    violations = sorted(set(data.keys()) & FORBIDDEN_KEYS)
    if violations:
        return _reject("protocol_violation:" + ",".join(violations))

    # (3) the only decision field must be present and well-typed
    task_id = data.get("selected_task_id")
    if not isinstance(task_id, str) or not task_id:
        return _reject("missing_selected_task_id")

    # (4) THE invariant: the choice must be a member of the set we sent.
    if task_id not in allowed:
        return _reject(f"task_not_in_candidates:{task_id}")

    reason = _clean_reason(data.get("reason"))
    alternatives = _clean_alternatives(data.get("alternatives"), allowed,
                                       exclude_id=task_id)

    return AgentPick(task_id=task_id, source=SOURCE_LLM, reason=reason,
                     confidence=_clamp(data.get("confidence")),
                     alternatives=alternatives)


# ===================================================================== #
# deterministic fallback (always available)
# ===================================================================== #
def fallback_pick(candidates: List, engine: TaskQueueEngine = None,
                  only_source: str = None) -> AgentPick:
    """Reproduce the plain queue ordering over the same candidate set.

    Used whenever the model is unavailable or its answer was rejected.
    Because it delegates to `TaskQueueEngine.next_task`, the fallback can
    never select a task outside the candidates — the day the agent is
    down, the operator still gets the same, explainable ordering as
    before the agent existed.
    """
    allowed = _candidate_ids(candidates, only_source)
    if not allowed:
        return AgentPick(source=SOURCE_EMPTY, rejected_reason="no_candidates")
    chosen = (engine or TASK_ENGINE).next_task(candidates, only_source)
    if chosen is None:                                   # defensive
        return AgentPick(source=SOURCE_EMPTY, rejected_reason="no_candidates")
    return AgentPick(task_id=chosen.task_id, source=SOURCE_FALLBACK,
                     reason=_fallback_reason(), confidence=0.0)
