from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Trigger:
    id: str
    keywords: list[str]
    template_key: str
    template_type: str
    priority: str
    description: str | None = None


_PRIORITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def load_triggers(path: Path) -> tuple[list[Trigger], dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    triggers_raw = raw.get("triggers") or []
    triggers: list[Trigger] = []
    for t in triggers_raw:
        if not isinstance(t, dict):
            continue
        triggers.append(
            Trigger(
                id=str(t.get("id") or ""),
                keywords=[str(k) for k in (t.get("keywords") or []) if str(k).strip()],
                template_key=str(t.get("template_key") or ""),
                template_type=str(t.get("template_type") or ""),
                priority=str(t.get("priority") or "low"),
                description=(str(t.get("description")) if t.get("description") is not None else None),
            )
        )
    return triggers, raw


def load_templates_bundle(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def match_trigger(triggers: list[Trigger], message: str) -> tuple[Trigger | None, dict[str, Any]]:
    text = _normalize(message)
    best: tuple[int, int, Trigger] | None = None  # (hits, priority_weight, trigger)
    for t in triggers:
        if not t.id or not t.template_key:
            continue
        hits = 0
        for kw in t.keywords:
            nkw = _normalize(kw)
            if nkw and nkw in text:
                hits += 1
        if hits <= 0:
            continue
        pw = _PRIORITY_WEIGHT.get(t.priority.lower().strip(), 1)
        cand = (hits, pw, t)
        if best is None or cand > best:
            best = cand
    if best is None:
        return None, {"hits": 0}
    hits, pw, t = best
    return t, {"hits": hits, "priority_weight": pw}


_VAR_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def render_template(bundle: dict[str, Any], template_type: str, template_key: str) -> dict[str, Any] | None:
    variables = bundle.get("variables") or {}
    group = bundle.get(template_type) or {}
    node = group.get(template_key)
    if not isinstance(node, dict):
        return None
    text = str(node.get("text") or "")

    def repl(m: re.Match[str]) -> str:
        k = m.group(1)
        v = variables.get(k)
        return str(v) if v is not None else m.group(0)

    rendered = _VAR_RE.sub(repl, text)
    out = dict(node)
    out["text"] = rendered
    return out

