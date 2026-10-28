# -*- coding: utf-8 -*-
# Copyright (C) 2026 jeffery9@gmail.com & geninit
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)
"""Lightweight declarative parameter schema (no external dependency).

Replaces jsonschema so the addon keeps zero non-stdlib dependencies.
Supports the primitive types needed by field operations: int, float,
str, bool, list, and enum-of-str. Extra keys are rejected (strict mode) so a
rogue LLM output cannot smuggle arbitrary fields into business calls.
"""
import re
from typing import Any, Dict, Mapping, Optional

_TYPES = {
    "int": int,
    "float": (int, float),  # ints are valid floats
    "str": str,
    "bool": bool,
    "list": list,
}

# A `list` field must contain JSON arrays of objects only. Without this a
# model could satisfy the type with `[1, 2]` or `["x"]` and the executor
# would then index into an int. Nested values are validated shallowly: the
# executor owns the meaning of each element, this only guarantees shape.
_LIST_ITEM = dict

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ValidationError(ValueError):
    """Raised when a value does not match its declared schema."""

    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(f"{field}: {message}")


class Field:
    __slots__ = ("name", "type", "required", "enum", "min_val", "max_val", "default", "help")

    def __init__(self, name, type_="str", required=True, enum=None,
                 min_val=None, max_val=None, default=None, help=""):
        if type_ not in _TYPES:
            raise ValueError(f"unsupported type {type_!r}")
        if not _NAME_RE.match(name):
            raise ValueError(f"invalid field name {name!r}")
        if enum is not None:
            if not isinstance(enum, (list, tuple)) or not enum:
                raise ValueError("enum must be a non-empty list")
        self.name = name
        self.type = type_
        self.required = required
        self.enum = tuple(enum) if enum else None
        self.min_val = min_val
        self.max_val = max_val
        self.default = default
        self.help = help

    def validate(self, value):
        """Return a coerced/validated value or raise ValidationError."""
        py_type = _TYPES[self.type]
        if self.type == "float" and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, py_type) or isinstance(value, bool) != (self.type == "bool"):
            raise ValidationError(self.name, f"expected {self.type}, got {type(value).__name__}")
        if self.type == "list":
            # `str` and `bytes` are not lists here even though they are
            # sequences, and an element that is not a mapping would break
            # the executor that reads keys off it.
            for item in value:
                if not isinstance(item, _LIST_ITEM):
                    raise ValidationError(
                        self.name,
                        "list items must be objects, got "
                        f"{type(item).__name__}")
        if self.enum is not None and value not in self.enum:
            raise ValidationError(self.name, f"value {value!r} not in {list(self.enum)}")
        if self.min_val is not None and value < self.min_val:
            raise ValidationError(self.name, f"value {value} < min {self.min_val}")
        if self.max_val is not None and value > self.max_val:
            raise ValidationError(self.name, f"value {value} > max {self.max_val}")
        return value

    def to_dict(self):
        d = {"name": self.name, "type": self.type, "required": self.required}
        if self.enum:
            d["enum"] = list(self.enum)
        if self.help:
            d["help"] = self.help
        return d


class ParamSchema:
    """A strict schema: unknown keys are rejected, missing required keys raise."""

    def __init__(self, fields):
        self.fields = {f.name: f for f in fields}

    def validate(self, data: Mapping[str, Any], *, ignore_unknown: bool = False) -> Dict[str, Any]:
        if not isinstance(data, Mapping):
            raise ValidationError("_root", "params must be a JSON object")
        unknown = set(data) - set(self.fields)
        if unknown and not ignore_unknown:
            raise ValidationError(sorted(unknown)[0], f"unknown parameter(s): {sorted(unknown)}")
        out = {}
        for name, field in self.fields.items():
            if name not in data or data[name] is None:
                if field.required and field.default is None:
                    raise ValidationError(name, "missing required parameter")
                if field.default is not None:
                    out[name] = field.default
                continue
            out[name] = field.validate(data[name])
        return out

    def to_dict(self):
        return [f.to_dict() for f in self.fields.values()]

    def prompt_fragment(self) -> str:
        """Compact description injected into LLM prompts so the model knows
        exactly which capability parameters it may produce."""
        lines = []
        for f in self.fields.values():
            spec = f"{f.name}:{f.type}"
            if f.enum:
                spec += f" enum={list(f.enum)}"
            if not f.required:
                spec += " optional"
            lines.append(f"- {spec}" + (f"  # {f.help}" if f.help else ""))
        return "\n".join(lines)
