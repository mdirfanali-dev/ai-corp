"""Per-run caps: which model, how many turns, how long. Bounded so a typo cannot create a runaway loop.

Order of precedence: the employee's own profile (``model``, ``max_turns``, ``timeout``) > the Founder's per-tier
settings (Usage page) > the built-in defaults.
"""
from __future__ import annotations

import sqlite3

from . import notify

ALLOWED_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
TURNS = (1, 40)
TIMEOUT = (30, 1800)
DEFAULTS = {
    "0": {"model": "claude-haiku-5-5", "max_turns": 6, "timeout": 300},
    "1": {"model": "claude-sonnet-5-5", "max_turns": 12, "timeout": 600},
    "2": {"model": "claude-sonnet-5-5", "max_turns": 16, "timeout": 900},
    "3": {"model": "claude-sonnet-5-5", "max_turns": 12, "timeout": 600},
}


def tier_settings(con: sqlite3.Connection) -> dict:
    saved = notify.get_setting(con, "run_limits") or {}
    return {t: {**DEFAULTS[t], **(saved.get(t) or {})} for t in DEFAULTS}


def _clamp(value, bounds, default):
    try:
        return max(bounds[0], min(bounds[1], int(value)))
    except (TypeError, ValueError):
        return default


def resolve(con: sqlite3.Connection, task: dict, employee: dict, run_tier: int | None = None) -> dict:
    tier = str(min(int(run_tier if run_tier is not None else task["risk_tier"]), 3))
    base = tier_settings(con)[tier]
    profile = employee.get("profile") or {}
    model = profile.get("model") if profile.get("model") in ALLOWED_MODELS else base["model"]
    return {"model": model if model in ALLOWED_MODELS else DEFAULTS[tier]["model"],
            "max_turns": _clamp(profile.get("max_turns") or base["max_turns"], TURNS, DEFAULTS[tier]["max_turns"]),
            "timeout": _clamp(profile.get("timeout") or base["timeout"], TIMEOUT, DEFAULTS[tier]["timeout"])}


def save_tier_settings(con: sqlite3.Connection, form: dict) -> dict:
    out = {}
    for tier in DEFAULTS:
        model = form.get(f"model_{tier}")
        out[tier] = {"model": model if model in ALLOWED_MODELS else DEFAULTS[tier]["model"],
                     "max_turns": _clamp(form.get(f"turns_{tier}"), TURNS, DEFAULTS[tier]["max_turns"]),
                     "timeout": _clamp(form.get(f"timeout_{tier}"), TIMEOUT, DEFAULTS[tier]["timeout"])}
    notify.set_setting(con, "run_limits", out)
    return out
