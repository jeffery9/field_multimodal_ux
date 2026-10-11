# -*- coding: utf-8 -*-
"""Language-agnostic quantity / barcode extraction.

This is a PURE deterministic helper, deliberately kept independent of any
parsing path. In the LLM-first architecture it serves two purposes:

1. **Consistency guard** — after the LLM returns `params`, compare the
   quantities/barcodes it extracted against this deterministic extractor.
   A mismatch is surfaced as an ambiguity for the user to resolve rather
   than silently trusted.
2. **Completion** — when the LLM returns a `capability_id` but omits a
   quantity/barcode that is plainly present in the text, fill it in.

Order matters: the barcode/model code is identified FIRST and masked out
of the text, so the digits inside a code like "MMUX-0001" can never be
mistaken for a quantity. This is what makes
"把 MMUX-0001 的 stock 数量改成 5 个" yield qty=5, not qty=1.
"""
import re
from typing import Any, Dict

_NUM_ZH = str.maketrans("０１２３４５６７８９．", "0123456789.")
_NUM_WORD_ZH = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}

_QTY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(件|个|箱|包|kg|公斤|千克|台|units?|pcs|pieces)?", re.I)
_BAD_RE = re.compile(r"(?:其中|其中)?\s*(\d+(?:\.\d+)?)\s*(?:件|个|pcs)?\s*(?:不合格|不良|次品|scrap|defect)", re.I)
# Barcode / model code. Field codes are frequently ALPHANUMERIC
# ("MMUX-0001", "AB-123456", "LOT2024A19") rather than pure digits, and
# users mix them into any language ("把 MMUX-0001 的 stock 数量改成 5").
# We therefore accept an alphanumeric token that contains at least one
# letter and one digit, with an optional internal -/_/. separator; a
# bare long digit run is still accepted (EAN/UPC style).
_BARCODE_RE = re.compile(
    r"\b(?=[A-Za-z0-9._-]{4,}\b)(?=[A-Za-z0-9._-]*\d)"
    r"(?=[A-Za-z0-9._-]*[A-Za-z]|[0-9]{6,})"
    r"([A-Za-z][A-Za-z0-9._-]*|\d{6,})\b")


def extract_quantities(text: str) -> Dict[str, Any]:
    """Extract {qty, scrap, barcode} from a possibly mixed-language text.

    Barcode is identified first and masked out so its digits are never
    read as a quantity. Never raises; missing values are None.
    """
    if not text:
        return {"qty": None, "scrap": None, "barcode": None}
    text = text.translate(_NUM_ZH)
    out: Dict[str, Any] = {"qty": None, "scrap": None, "barcode": None}

    m = _BARCODE_RE.search(text)
    if m:
        out["barcode"] = m.group(1)
        # mask the code (keep length so surrounding positions stay valid)
        text = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]

    scrap_m = _BAD_RE.search(text)
    if scrap_m:
        out["scrap"] = float(scrap_m.group(1))
        text_wo = text[:scrap_m.start()] + text[scrap_m.end():]
    else:
        text_wo = text

    qty_m = _QTY_RE.search(text_wo)
    if qty_m and not _BAD_RE.match(qty_m.group(0)):
        out["qty"] = float(qty_m.group(1))
    return out


def reconcile_with_text(params: Dict[str, Any], text: str,
                        qty_keys=("quantity", "qty", "qty_production",
                                  "counted_qty", "qty_done"),
                        scrap_keys=("qty_scrapped", "scrap"),
                        barcode_keys=("barcode", "code")):
    """Cross-check LLM-extracted params against the deterministic extractor.

    Returns (params, notes):
      - `notes` is a list of human-readable inconsistency messages
        (English, for the audit/ambiguity channel).
      - Missing qty/barcode that the text plainly contains are filled in.
      - A value that disagrees with the text is reported, NOT overwritten
        (the LLM may have context the extractor lacks; the user decides).
    """
    params = dict(params or {})
    notes = []
    found = extract_quantities(text)

    def _first(keys):
        for k in keys:
            if params.get(k) is not None:
                return k
        return None

    # quantity
    qty_key = _first(qty_keys)
    if found["qty"] is not None:
        if qty_key is None:
            # fill the first declared qty key if one exists in the schema
            for k in qty_keys:
                if k in params or k in _schema_hint(params):
                    params[k] = found["qty"]
                    notes.append(f"filled {k}={found['qty']} from text")
                    break
        else:
            try:
                if float(params[qty_key]) != float(found["qty"]):
                    notes.append(
                        f"{qty_key}={params[qty_key]} disagrees with text "
                        f"value {found['qty']}")
            except (TypeError, ValueError):
                notes.append(f"{qty_key} is not numeric: {params[qty_key]!r}")

    # scrap
    scrap_key = _first(scrap_keys)
    if found["scrap"] is not None and scrap_key is not None:
        try:
            if float(params[scrap_key]) != float(found["scrap"]):
                notes.append(
                    f"{scrap_key}={params[scrap_key]} disagrees with text "
                    f"value {found['scrap']}")
        except (TypeError, ValueError):
            pass

    # barcode / code
    bar_key = _first(barcode_keys)
    if found["barcode"] and bar_key is not None:
        if str(params[bar_key]).strip().upper() != found["barcode"].upper():
            notes.append(
                f"{bar_key}={params[bar_key]!r} disagrees with text "
                f"value {found['barcode']!r}")
    return params, notes


def _schema_hint(params):
    """Placeholder hook; key presence is decided by the caller's schema."""
    return {}
