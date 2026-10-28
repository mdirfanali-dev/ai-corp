"""What the work costs in tokens: per employee, team, model and day, and how much the context pack saves.

Token numbers come from the Claude Code result (when the runner reports them); prompt sizes are measured in
characters of what was actually sent, against the naive baseline defined in ``context.naive_size``. Converting
characters to tokens uses the rough rule of 4 characters per token, which is an *assumption*, not a measurement.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

CHARS_PER_TOKEN = 4


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _saving(sent: int, base: int) -> float | None:
    return round(100.0 * (1 - sent / base), 1) if base else None


def summary(con: sqlite3.Connection, days: int = 30) -> dict:
    since = _since(days)
    total = con.execute("SELECT count(*) runs, COALESCE(sum(est_tokens),0) tokens, COALESCE(sum(prompt_chars),0) sent, COALESCE(sum(baseline_chars),0) base, "
                        "count(prompt_chars) measured FROM usage WHERE ts >= ?", (since,)).fetchone()
    by_emp = [dict(r) for r in con.execute(
        "SELECT COALESCE(e.name, '(unknown)') name, count(*) runs, COALESCE(sum(u.est_tokens),0) tokens, COALESCE(sum(u.prompt_chars),0) sent, "
        "COALESCE(sum(u.baseline_chars),0) base FROM usage u LEFT JOIN employee e ON e.emp_id = u.emp_id WHERE u.ts >= ? GROUP BY u.emp_id ORDER BY tokens DESC, runs DESC", (since,))]
    by_team = [dict(r) for r in con.execute(
        "SELECT t.name name, count(*) runs, COALESCE(sum(u.est_tokens),0) tokens FROM usage u JOIN team_member m ON m.emp_id = u.emp_id "
        "JOIN team t ON t.team_id = m.team_id AND t.deleted_at IS NULL WHERE u.ts >= ? GROUP BY t.team_id ORDER BY tokens DESC", (since,))]
    by_model = [dict(r) for r in con.execute(
        "SELECT COALESCE(model, '(unknown)') name, count(*) runs, COALESCE(sum(est_tokens),0) tokens FROM usage WHERE ts >= ? GROUP BY model ORDER BY tokens DESC", (since,))]
    by_day = [dict(r) for r in con.execute(
        "SELECT substr(ts, 1, 10) day, count(*) runs, COALESCE(sum(est_tokens),0) tokens FROM usage WHERE ts >= ? GROUP BY day ORDER BY day DESC LIMIT 14", (since,))]
    for row in by_emp:
        row["saving"] = _saving(row["sent"], row["base"])
    return {"days": days, "runs": total["runs"], "tokens": total["tokens"], "measured_runs": total["measured"], "prompt_chars": total["sent"],
            "baseline_chars": total["base"], "saving": _saving(total["sent"], total["base"]),
            "prompt_tokens_est": total["sent"] // CHARS_PER_TOKEN, "baseline_tokens_est": total["base"] // CHARS_PER_TOKEN,
            "by_employee": by_emp, "by_team": by_team, "by_model": by_model, "by_day": by_day}
