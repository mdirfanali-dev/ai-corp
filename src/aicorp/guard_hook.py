#!/usr/bin/env python3
"""Claude Code PreToolUse hook. Runs as the worker user, before every tool call.

Usage (configured by the runner):  python3 guard_hook.py <policy.json>
Reads the tool call as JSON on stdin. Exit 0 = allow. Exit 2 + message on stderr = block.
Any problem (unreadable policy, bad input, unexpected error) blocks: the hook fails closed.
Needs only ``aicorp_policy.py`` next to it (a copy of policy.py).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    try:
        import aicorp_policy as policy

        with open(sys.argv[1], encoding="utf-8") as fh:
            rules = policy.Policy.from_json(fh.read())
        call = json.load(sys.stdin)
        decision = policy.decide(rules, call.get("tool_name", ""), call.get("tool_input"))
    except Exception as err:  # fail closed
        print(f"BLOCKED by AICorp policy [guard.error]: {type(err).__name__}", file=sys.stderr)
        return 2
    if decision.allow:
        return 0
    print(decision.message(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
