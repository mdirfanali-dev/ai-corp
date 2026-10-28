"""The plan of a browser task: plain data, validated before anything is run.

A browser worker is a deterministic script runner - there is no AI in the app. The task description holds a JSON
plan (optionally inside a ```json fence)::

    {"steps": [{"goto": "https://example.org/"}, {"extract": {"selector": "main", "name": "overview"}}, {"screenshot": "home"}]}

Read-only verbs (goto, extract, screenshot, wait, wait_for) need risk tier >= 1; verbs that act on a page
(click, fill, download, ask) need tier >= 2. Passwords are never typed by a plan: log in by hand during a hand-off.
"""
from __future__ import annotations

import json
import re

MAX_STEPS = 40
READ_VERBS = ("goto", "extract", "screenshot", "wait", "wait_for")
ACT_VERBS = ("click", "fill", "download", "ask")
SECRETISH = re.compile(r"passw|passwd|pwd|secret|token|otp|cvv|card", re.I)


class PlanError(ValueError):
    pass


def _source(text: str) -> str:
    text = (text or "").strip()
    fence = re.search(r"```(?:json|browser)?\s*(\{.*?\})\s*```", text, re.S)
    return fence.group(1) if fence else text


def _name(value, default: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or default)).strip("-.")[:40]
    return value or default


def _selector(value, verb: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise PlanError(f"{verb}: a selector is required")
    if SECRETISH.search(value):
        raise PlanError(f"{verb}: plans never touch password, card or code fields - log in by hand during a hand-off")
    return value.strip()


def parse(text: str) -> list[dict]:
    """Return normalised steps ``[{"verb":..., ...}]`` or raise PlanError with a plain-English reason."""
    src = _source(text)
    if not src.startswith("{"):
        raise PlanError("A browser task needs a JSON plan in its description: {\"steps\": [{\"goto\": \"https://...\"}, ...]}")
    try:
        data = json.loads(src)
    except ValueError as err:
        raise PlanError(f"The plan is not valid JSON ({err.__class__.__name__})") from None
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list) or not steps:
        raise PlanError("The plan needs a non-empty \"steps\" list")
    if len(steps) > MAX_STEPS:
        raise PlanError(f"At most {MAX_STEPS} steps per task")
    out: list[dict] = []
    for i, raw in enumerate(steps, 1):
        if not isinstance(raw, dict) or len(raw) != 1:
            raise PlanError(f"Step {i}: each step is an object with exactly one action")
        verb, arg = next(iter(raw.items()))
        if verb not in READ_VERBS + ACT_VERBS:
            raise PlanError(f"Step {i}: unknown action '{verb}' (allowed: {', '.join(READ_VERBS + ACT_VERBS)})")
        step: dict = {"verb": verb}
        if verb == "goto":
            if not isinstance(arg, str) or not arg.strip():
                raise PlanError(f"Step {i}: goto needs a URL")
            step["url"] = arg.strip()
        elif verb == "extract":
            arg = {"selector": arg} if isinstance(arg, str) else (arg or {})
            step.update(selector=_selector(arg.get("selector", "body"), "extract"), name=_name(arg.get("name"), f"page{i}"))
        elif verb == "screenshot":
            step["name"] = _name(arg if isinstance(arg, str) else (arg or {}).get("name") if isinstance(arg, dict) else None, f"shot{i}")
        elif verb == "wait":
            if not isinstance(arg, (int, float)) or not 0 < arg <= 60:
                raise PlanError(f"Step {i}: wait takes a number of seconds (1-60)")
            step["seconds"] = float(arg)
        elif verb == "wait_for":
            step["selector"] = _selector(arg, "wait_for")
        elif verb == "click":
            step["selector"] = _selector(arg, "click")
        elif verb == "fill":
            if not isinstance(arg, dict) or not isinstance(arg.get("text"), str):
                raise PlanError(f"Step {i}: fill takes {{\"selector\": ..., \"text\": ...}}")
            if len(arg["text"]) > 2000:
                raise PlanError(f"Step {i}: text is too long")
            step.update(selector=_selector(arg.get("selector"), "fill"), text=arg["text"])
        elif verb == "download":
            if not isinstance(arg, dict):
                raise PlanError(f"Step {i}: download takes {{\"click\": selector, \"name\": ...}}")
            step.update(selector=_selector(arg.get("click"), "download"), name=_name(arg.get("name"), f"file{i}"))
        elif verb == "ask":
            if not isinstance(arg, dict) or not isinstance(arg.get("prompt"), str) or not arg.get("site"):
                raise PlanError(f"Step {i}: ask takes {{\"site\": ..., \"prompt\": ...}}")
            if len(arg["prompt"]) > 4000:
                raise PlanError(f"Step {i}: prompt is too long")
            step.update(site=_name(arg["site"], "site"), prompt=arg["prompt"], name=_name(arg.get("name"), f"answer{i}"))
        out.append(step)
    return out


def required_tier(steps: list[dict]) -> int:
    return 2 if any(s["verb"] in ACT_VERBS for s in steps) else 1


def validate_for_task(text: str, risk_tier: int) -> list[dict]:
    steps = parse(text)
    need = required_tier(steps)
    if risk_tier < need:
        raise PlanError(f"This plan clicks, fills, downloads or asks a site, which needs risk tier {need} (this task is tier {risk_tier})")
    return steps
