"""Deterministic checks for text that is about to reach an agent.

This is a first filter, not a wall: regular expressions cannot recognise every attack. The real
protection is the tool policy, the hook and the operating-system user (see policy.py). Matches are
logged so the Founder can see what was attempted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_I = re.IGNORECASE | re.DOTALL

RULES: list[tuple[str, re.Pattern]] = [
    ("override_instructions", re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|any|your|these|the)\b"
        r".{0,30}\b(instructions?|rules?|guardrails?|polic(?:y|ies)|system prompt|restrictions?|safety)\b", _I)),
    ("role_hijack", re.compile(
        r"\b(you are now|act as|pretend to be|from now on you)\b.{0,50}\b(dan|unrestricted|jailbroken|"
        r"no restrictions|without (?:any )?(?:rules|restrictions|limits)|developer mode)\b", _I)),
    ("jailbreak_keyword", re.compile(r"\b(jailbreak|developer mode enabled|do anything now)\b", _I)),
    ("reveal_system_prompt", re.compile(
        r"\b(reveal|print|show|repeat|output|leak)\b.{0,30}\b(system prompt|hidden instructions|your instructions)\b", _I)),
    ("disable_guardrails", re.compile(
        r"\b(disable|turn off|deactivate|circumvent|evade|bypass)\b.{0,30}\b(guardrails?|safety (?:checks?|filters?|rules?|measures?)|"
        r"content polic(?:y|ies)|hooks?|sandbox|approval (?:gate|step|requirement)|kill switch|permission (?:checks?|system))\b", _I)),
    ("secret_files", re.compile(
        r"(?<!\w)(secrets?\.env|\.env\b|id_rsa|id_ed25519|\.ssh\b|\.aws\b|company\.sqlite|browser-profiles)", _I)),
    ("exfiltrate_credentials", re.compile(
        r"\b(send|email|upload|post|exfiltrate|leak|dump|transmit|forward)\b.{0,60}\b(passwords?|tokens?|api[_ -]?keys?|"
        r"app password|credentials?|private keys?|cookies?)\b", _I)),
    ("destructive_shell", re.compile(
        r"(\brm\s+-[a-z]*[rf][a-z]*\b|\bmkfs(\.\w+)?\b|\bdd\s+if=|:\(\)\s*\{|\bchmod\s+-R\s+0?777\s+/|>\s*/dev/sd[a-z]|"
        r"\bshred\s+-|\bwipefs\b|\bformat\s+[a-z]:)", _I)),
    ("destructive_sql", re.compile(r"\b(drop|truncate)\s+(table|database|schema)\b", _I)),
    ("remote_code", re.compile(
        r"((curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba|z|da)?sh\b|base64\s+(-d|--decode)[^|\n]*\|\s*(ba|z)?sh|"
        r"bash\s+-i\s*>&?\s*/dev/tcp|\bnc\s+-e\b|powershell\s+-(enc|encodedcommand)\b|\beval\s*\(\s*base64)", _I)),
    ("account_takeover", re.compile(
        r"\b(change|reset|update)\b.{0,20}\b(password|2fa|two[- ]factor|recovery email)\b.{0,30}\b(account|gmail|github|bank)\b", _I)),
]

# Markup that tries to impersonate the conversation structure inside untrusted text.
_ROLE_MARKUP = re.compile(r"(<\|[^|>]{1,40}\|>|^\s*(system|assistant|developer)\s*:|\[/?INST\]|</?system>)", re.I | re.M)


@dataclass
class Verdict:
    level: str                                   # "ok" or "block"
    rules: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.level == "block"

    def describe(self) -> str:
        return ", ".join(self.rules)


def check(text: str | None) -> Verdict:
    """Scan text. ``block`` means do not hand it to an agent."""
    if not text:
        return Verdict("ok")
    hits = [name for name, pattern in RULES if pattern.search(text)]
    return Verdict("block" if hits else "ok", hits)


def neutralize(text: str) -> str:
    """Defuse conversation-structure markup inside untrusted text (kept readable, no longer markup)."""
    return _ROLE_MARKUP.sub(lambda m: "[removed markup]", text)


def quarantine(text: str, label: str = "content") -> tuple[str, Verdict]:
    """For untrusted material (web pages, messages, other workers' output): if it matches a block
    rule, replace it entirely with a notice; otherwise neutralise markup and pass it on as data."""
    verdict = check(text)
    if verdict.blocked:
        return f"[{label} removed by the input guard: {verdict.describe()}]", verdict
    return neutralize(text), verdict
