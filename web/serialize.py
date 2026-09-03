"""Tolerant serialization helpers.

The web layer must not care whether the object it was handed is an
``engine.events.Event`` dataclass, something with ``to_dict()``, or a plain
dict (fakes in tests). Everything funnels through here.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any


def to_jsonable(obj: Any) -> Any:
    """Best-effort conversion of an arbitrary object into JSON-safe data."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return to_jsonable(to_dict())
        except Exception:  # pragma: no cover - defensive
            pass
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(dataclasses.asdict(obj))
    if hasattr(obj, "__dict__"):
        return {str(k): to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def event_to_dict(ev: Any) -> dict[str, Any]:
    """Normalize an Event into the wire shape the UI consumes.

    Always has: seq, round, kind, actor, text, data.
    """
    if isinstance(ev, dict):
        d = dict(ev)
    else:
        d = to_jsonable(ev)
        if not isinstance(d, dict):
            d = {"text": str(ev)}
    out: dict[str, Any] = {
        "seq": int(d.get("seq") or 0),
        "round": int(d.get("round") or 0),
        "kind": str(d.get("kind") or "system"),
        "actor": d.get("actor"),
        "text": d.get("text") or "",
        "data": to_jsonable(d.get("data") or {}),
    }
    for k, v in d.items():
        if k not in out:
            out[k] = to_jsonable(v)
    return out


def dumps(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), separators=(",", ":"), default=str)
