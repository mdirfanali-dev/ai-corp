"""The skill registry: ``skills/<name>/SKILL.md`` folders, with metadata, scoping and vetting.

A skill is know-how in Markdown that is added to an agent's prompt *only* when the task needs it
(see ``context.pick_skills``). Because the text goes into a prompt, a skill from outside is treated like code:
it must be **vetted** by the Founder before any agent sees it, and a vetted skill that changes is unvetted again
(the vetting stores the SHA-256 of what was checked). Skills written here ("own") need no vetting.

Optional front matter::

    ---
    name: market-research
    description: How to research a market quickly
    version: 1.2
    source: own            # own | a URL or author for vendor skills
    license: MIT
    employees: Aria, Ben   # who may use it (names or ids); empty = everyone who lists it
    keywords: market, competitor, pricing
    ---
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import events, inputguard, trash
from .config import utc_now
from .org import FOUNDER, ValidationError, authorize

NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
MAX_SKILL_BYTES = 20_000
DANGEROUS = [
    (re.compile(r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", re.I), "pipes a download into a shell"),
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", re.I), "recursive forced delete"),
    (re.compile(r"\bsudo\b", re.I), "asks for administrator rights"),
    (re.compile(r"chmod\s+(-R\s+)?7[0-7][0-7]\b", re.I), "opens file permissions wide"),
    (re.compile(r"secrets?\.env|\.ssh/|id_rsa|\.aws/|credentials", re.I), "mentions credential files"),
    (re.compile(r"base64\s+(-d|--decode)|eval\s*\(|exec\s*\(", re.I), "decodes or executes hidden code"),
    (re.compile(r"(disable|ignore|bypass|skip)\s+(the\s+)?(safety|guardrail|policy|hook|permission)", re.I), "tells the agent to bypass safety rules"),
]
URL = re.compile(r"https?://[^\s)>\]\"']+", re.I)


def _dir(home: Path) -> Path:
    return home / "skills"


def parse(text: str) -> tuple[dict, str]:
    """Front matter (simple ``key: value`` lines) and body."""
    meta: dict = {}
    body = text
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition(":")
                meta[key.strip().lower()] = re.sub(r"\s+#.*$", "", value).strip()
        body = text[m.end():]
    return meta, body


def _list(value: str | None) -> list[str]:
    return [x.strip() for x in re.split(r"[,;]", value or "") if x.strip()]


def scan(text: str) -> list[dict]:
    """Automatic part of the vetting checklist. ``block`` findings stop vetting; ``info`` ones are for the human reader."""
    findings: list[dict] = []
    if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
        findings.append({"level": "block", "text": f"larger than {MAX_SKILL_BYTES // 1000} KB (skills must be short: every byte costs tokens)"})
    verdict = inputguard.check(text)
    if verdict.blocked:
        findings.append({"level": "block", "text": f"matches the input guard: {verdict.describe()}"})
    for pattern, why in DANGEROUS:
        if pattern.search(text):
            findings.append({"level": "block", "text": why})
    if events.scrub(text) != text:
        findings.append({"level": "block", "text": "contains a value that looks like one of your secrets"})
    urls = sorted({re.sub(r"[.,;]+$", "", u) for u in URL.findall(text)})
    if urls:
        findings.append({"level": "info", "text": "links to: " + ", ".join(u[:80] for u in urls[:6]) + (" ..." if len(urls) > 6 else "")})
    return findings


def _read(home: Path, name: str) -> tuple[str, Path] | None:
    safe = re.sub(r"[^A-Za-z0-9._-]", "", name)
    path = _dir(home) / safe / "SKILL.md"
    if not safe or not path.is_file() or path.is_symlink():
        return None
    return path.read_text(encoding="utf-8", errors="replace"), path


def describe(home: Path, con: sqlite3.Connection, name: str) -> dict | None:
    got = _read(home, name)
    if got is None:
        return None
    text, path = got
    meta, body = parse(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    row = con.execute("SELECT * FROM skill_vetting WHERE name = ?", (path.parent.name,)).fetchone()
    source = (meta.get("source") or "own").strip() or "own"
    own = source.lower() == "own"
    if own:
        status = "own"
    elif row is None:
        status = "unvetted"
    elif row["sha256"] != digest:
        status = "changed"
    else:
        status = row["status"]
    first = next((l.strip("# ").strip() for l in body.splitlines() if l.strip()), "")
    return {"name": path.parent.name, "description": meta.get("description") or first[:140], "version": meta.get("version") or "",
            "source": source, "license": meta.get("license") or "", "employees": _list(meta.get("employees")),
            "keywords": _list(meta.get("keywords")), "bytes": len(text.encode("utf-8")), "sha256": digest, "status": status,
            "findings": scan(text), "vetted_by": row["vetted_by"] if row else None, "vetted_at": row["vetted_at"] if row else None,
            "body": body, "text": text}


def list_skills(home: Path, con: sqlite3.Connection) -> list[dict]:
    root = _dir(home)
    if not root.is_dir():
        return []
    return [d for d in (describe(home, con, p.name) for p in sorted(root.iterdir()) if p.is_dir()) if d]


def usable(home: Path, con: sqlite3.Connection, name: str, employee: dict | None = None) -> tuple[bool, str]:
    """May an agent be given this skill? (Vetting and scoping; the keyword match is a separate, later step.)"""
    info = describe(home, con, name)
    if info is None:
        return False, "no such skill"
    if info["status"] not in ("own", "vetted"):
        return False, {"unvetted": "not vetted yet", "changed": "changed since it was vetted", "rejected": "rejected"}[info["status"]]
    if info["employees"] and employee is not None:
        allowed = {e.lower() for e in info["employees"]}
        if str(employee.get("name", "")).lower() not in allowed and str(employee.get("emp_id", "")).lower() not in allowed:
            return False, "not allowed for this employee"
    return True, "ok"


def checklist(info: dict) -> list[dict]:
    """The vetting checklist with the automatic answers filled in (the Founder confirms the manual ones)."""
    blocks = [f["text"] for f in info["findings"] if f["level"] == "block"]
    return [
        {"key": "scan", "text": "Automatic scan found nothing dangerous", "auto": True, "ok": not blocks, "detail": "; ".join(blocks)},
        {"key": "source", "text": "I know where this skill comes from", "auto": False, "ok": None, "detail": info["source"]},
        {"key": "license", "text": "The licence allows me to use it", "auto": False, "ok": None, "detail": info["license"] or "no licence stated"},
        {"key": "read", "text": "I read it and it only gives know-how (no commands to run, no rules to break)", "auto": False, "ok": None, "detail": ""},
    ]


def vet(home: Path, con: sqlite3.Connection, name: str, confirmed: set[str], note: str = "", actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    info = describe(home, con, name)
    if info is None:
        raise ValidationError("Skill not found")
    if info["status"] == "own":
        raise ValidationError("This is your own skill; it needs no vetting")
    items = checklist(info)
    problems = [i["text"] for i in items if (i["auto"] and not i["ok"]) or (not i["auto"] and i["key"] not in confirmed)]
    if problems:
        raise ValidationError("Not vetted. Open points: " + "; ".join(problems))
    con.execute("INSERT INTO skill_vetting (name, status, sha256, vetted_at, vetted_by, checklist, note) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT (name) DO UPDATE SET status = excluded.status, sha256 = excluded.sha256, vetted_at = excluded.vetted_at, "
                "vetted_by = excluded.vetted_by, checklist = excluded.checklist, note = excluded.note",
                (info["name"], "vetted", info["sha256"], utc_now(), actor["actor_name"], json.dumps(sorted(confirmed)), events.scrub(note)[:300]))
    events.append(con, type="skill.vetted", summary=f"Vetted skill {info['name']}", payload={"name": info["name"], "sha256": info["sha256"][:16]}, **actor)


def reject(home: Path, con: sqlite3.Connection, name: str, note: str = "", actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    info = describe(home, con, name)
    if info is None:
        raise ValidationError("Skill not found")
    con.execute("INSERT INTO skill_vetting (name, status, sha256, vetted_at, vetted_by, note) VALUES (?, 'rejected', ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET status = 'rejected', sha256 = excluded.sha256, vetted_at = excluded.vetted_at, vetted_by = excluded.vetted_by, note = excluded.note",
                (info["name"], info["sha256"], utc_now(), actor["actor_name"], events.scrub(note)[:300]))
    events.append(con, type="skill.rejected", summary=f"Rejected skill {info['name']}", payload={"name": info["name"]}, **actor)


def save(home: Path, con: sqlite3.Connection, data: dict, actor: dict = FOUNDER) -> str:
    """Create or replace a skill from the form. Anything not marked ``source: own`` must be vetted before it is used."""
    authorize(con, actor, founder_only=True)
    name = str(data.get("name") or "").strip().lower()
    if not NAME.fullmatch(name):
        raise ValidationError("Skill name: lowercase letters, digits, dot, dash or underscore (max 40)")
    body = events.scrub(str(data.get("body") or "")).strip()
    if not body:
        raise ValidationError("The skill needs some content")
    source = (str(data.get("source") or "own").strip() or "own")[:200]
    head = ["---", f"name: {name}", f"description: {re.sub(r'\s+', ' ', str(data.get('description') or ''))[:200]}",
            f"version: {re.sub(r'[^0-9A-Za-z.+-]', '', str(data.get('version') or '1'))[:20] or '1'}", f"source: {source}",
            f"license: {re.sub(r'\s+', ' ', str(data.get('license') or ''))[:60]}", f"employees: {re.sub(r'\s+', ' ', str(data.get('employees') or ''))[:300]}",
            f"keywords: {re.sub(r'\s+', ' ', str(data.get('keywords') or ''))[:300]}", "---", ""]
    text = "\n".join(head) + body + "\n"
    if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
        raise ValidationError(f"A skill is at most {MAX_SKILL_BYTES // 1000} KB")
    path = _dir(home) / name / "SKILL.md"
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    events.append(con, type="skill.saved", summary=f"{'Updated' if existed else 'Added'} skill {name} (source: {source})", payload={"name": name, "source": source}, **actor)
    return name


def remove(home: Path, con: sqlite3.Connection, name: str, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    got = _read(home, name)
    if got is None:
        raise ValidationError("Skill not found")
    trash.move_to_trash(home, got[1].parent, reason="skill removed", by=actor["actor_name"])
    con.execute("DELETE FROM skill_vetting WHERE name = ?", (got[1].parent.name,))
    events.append(con, type="skill.removed", summary=f"Removed skill {got[1].parent.name} (moved to .trash)", payload={"name": got[1].parent.name}, **actor)
