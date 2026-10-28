"""Runner that does real work with Claude Code, locked down in four layers:

1. the tool set is cut down to what the task's risk tier allows (``--tools``);
2. a PreToolUse hook (policy.py) checks every single call and fails closed;
3. the process runs as a separate low-privilege operating-system user that cannot read company data;
4. its whole output is captured, secrets are scrubbed, and every tool call is written to the audit log.

It uses the Founder's Claude subscription login; API-key variables are removed from its environment.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import context, inputguard, policy, redact
from .runners import RunContext, RunResult

ALLOWED_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
SCRUBBED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "OPENAI_", "GOOGLE_", "AZURE_")
SCRUBBED_NAMES = ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "NPM_TOKEN", "HF_TOKEN", "SSH_AUTH_SOCK")
KEPT_ENV = ("PATH", "HOME", "LANG", "LANGUAGE", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL", "TZ",
            "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA",
            "USERPROFILE", "TEMP", "TMP")
TIER_TOOLS = {0: ["Read", "Grep", "Glob"], 1: ["Read", "Grep", "Glob", "Write", "Edit"],
              2: ["Read", "Grep", "Glob", "Write", "Edit", "Bash"]}
WEB_TOOLS = ["WebSearch", "WebFetch"]
USAGE_RE = re.compile(r"usage limit|out of usage|rate.?limit|credits|limit reached|quota|too many requests", re.I)
LOGIN_RE = re.compile(r"not logged in|please run /login|/login|invalid (api )?key|authentication|oauth|unauthori[sz]ed|\b401\b", re.I)
MAX_EVENTS_PER_RUN = 150
MAX_ARTIFACTS = 100
MAX_ARTIFACT_BYTES = 5_000_000


class RunnerConfigError(RuntimeError):
    pass


@dataclass
class RunnerConfig:
    home: Path
    worker_user: str | None = None
    claude_bin: str = "claude"
    allow_same_user: bool = False
    max_turns: int = 12
    timeout: float = 600.0

    @classmethod
    def from_env(cls, home: Path) -> "RunnerConfig":
        env = os.environ
        return cls(home=home, worker_user=env.get("AICORP_WORKER_USER") or None,
                   claude_bin=env.get("AICORP_CLAUDE_BIN", "claude"),
                   allow_same_user=env.get("AICORP_ALLOW_SAME_USER") == "1",
                   max_turns=int(env.get("AICORP_MAX_TURNS", "12")),
                   timeout=float(env.get("AICORP_RUN_TIMEOUT", "600")))


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the agent process: a short allow-list, never keys or tokens."""
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if (k in KEPT_ENV or k.startswith("LC_"))
           and not k.startswith(SCRUBBED_PREFIXES) and k not in SCRUBBED_NAMES}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def choose_model(task: dict, employee: dict) -> str:
    """Lightest model that is fit for the job: read-only work on Haiku, everything else on Sonnet.
    An employee profile may name another model from the allowed list."""
    requested = (employee.get("profile") or {}).get("model")
    if requested in ALLOWED_MODELS:
        return requested
    return "claude-haiku-5-5" if task["risk_tier"] == 0 else "claude-sonnet-5-5"


def tool_target(name: str, tool_input: dict) -> str:
    for key in ("file_path", "notebook_path", "path", "command", "url", "pattern", "query"):
        if isinstance(tool_input.get(key), str):
            return tool_input[key][:200]
    return ""


@dataclass
class StreamState:
    tool_calls: int = 0
    blocked: list[dict] = field(default_factory=list)
    result: dict | None = None
    last_text: str = ""
    events_emitted: int = 0
    pending: dict[str, tuple[str, str]] = field(default_factory=dict)   # tool_use_id -> (tool, target)
    log_bytes: int = 0


class ClaudeCodeRunner:
    name = "claude-code"
    simulated = False

    def __init__(self, config: RunnerConfig) -> None:
        self.cfg = config
        self.home = config.home.resolve()
        if not config.worker_user and not config.allow_same_user:
            raise RunnerConfigError(
                "No worker user configured. Set AICORP_WORKER_USER to a low-privilege account (recommended; see the "
                "setup guide), or set AICORP_ALLOW_SAME_USER=1 to run agents as you with weaker isolation.")
        self.redactor = redact.Redactor(redact.load_secret_values(self.home))

    # ------------------------------------------------------------ preparation

    def _guard_dir(self) -> Path:
        guard = self.home / "runtime" / "guard"
        guard.mkdir(parents=True, exist_ok=True)
        package = Path(__file__).resolve().parent
        for source, target in (("policy.py", "aicorp_policy.py"), ("guard_hook.py", "guard_hook.py")):
            destination = guard / target
            data = (package / source).read_bytes()
            if not destination.exists() or destination.read_bytes() != data:
                destination.write_bytes(data)
            destination.chmod(0o644)
        for folder in (self.home / "runtime", guard):
            folder.chmod(0o755)
        return guard

    def _sudo(self, *args: str) -> list[str]:
        return ["sudo", "-n", "-u", self.cfg.worker_user, "-H", *args]

    def ensure_workspace(self, workspace: Path) -> None:
        if self.cfg.worker_user:
            subprocess.run(self._sudo("mkdir", "-p", str(workspace)), check=True, capture_output=True)
            subprocess.run(self._sudo("chmod", "2770", str(workspace)), check=True, capture_output=True)
        else:
            workspace.mkdir(parents=True, exist_ok=True)

    def _policy_for(self, workspace: Path, run_tier: int, employee: dict) -> policy.Policy:
        profile = employee.get("profile") or {}
        commands = [c for c in profile.get("commands", []) if c in policy.DEFAULT_COMMANDS] or list(policy.DEFAULT_COMMANDS)
        return policy.Policy(
            workspace=str(workspace), tier=run_tier, read_roots=[str(self.home / "skills")],
            deny_roots=[str(self.home / d) for d in ("config", "database", "backups", "company", ".trash",
                                                     "runtime", "memory", "workspaces")],
            web="web" in [t.lower() for t in profile.get("tools", [])], commands=commands)

    def build_command(self, ctx: RunContext, pack: context.PromptPack, run_tier: int, rules: policy.Policy,
                      model: str, token: str, max_turns: int | None = None, timeout: float | None = None) -> tuple[list[str], Path, Path]:
        guard = self._guard_dir()
        run_dir = self.home / "runtime" / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o755)
        policy_file = run_dir / f"{token}.policy.json"
        settings_file = run_dir / f"{token}.settings.json"
        policy_file.write_text(rules.to_json(), encoding="utf-8")
        python = "python3" if self.cfg.worker_user else sys.executable
        hook = f"{shlex.quote(python)} {shlex.quote(str(guard / 'guard_hook.py'))} {shlex.quote(str(policy_file))}"
        settings_file.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": hook}]}]}}), encoding="utf-8")
        for path in (policy_file, settings_file):
            path.chmod(0o644)
        tools = list(TIER_TOOLS[run_tier]) + (WEB_TOOLS if rules.web else [])
        core = [self.cfg.claude_bin, "-p", pack.prompt, "--model", model, "--tools", ",".join(tools),
                "--settings", str(settings_file), "--setting-sources", "user", "--permission-mode", "dontAsk",
                "--allowedTools", " ".join(tools), "--max-turns", str(max_turns or self.cfg.max_turns), "--no-session-persistence",
                "--strict-mcp-config", "--disable-slash-commands", "--append-system-prompt", pack.rules,
                "--output-format", "stream-json", "--verbose"]
        limit = [] if not shutil.which("timeout") else ["timeout", "--signal=TERM", "--kill-after=10",
                                                         str(int(timeout or self.cfg.timeout) + 5)]
        if self.cfg.worker_user:
            unset = [x for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                                 "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX") for x in ("-u", k)]
            command = self._sudo("env", *unset, *limit, *core)
        else:
            command = [*limit, *core]
        return command, policy_file, settings_file

    # ------------------------------------------------------------ the run

    def run(self, ctx: RunContext) -> RunResult:
        task, employee = ctx.task, ctx.employee
        tier = task["risk_tier"]
        if tier >= 4:
            return RunResult("blocked", "tier 4 work is never started automatically", block_reason="NEEDS_INPUT",
                             detail="Critical (tier 4) tasks are done by the Founder, not by agents.")
        verdict = inputguard.check(f"{task['title']}\n{task.get('description') or ''}")
        if verdict.blocked:
            ctx.emit("guard.input_blocked", f"Input guard stopped {task['title']}: {verdict.describe()}",
                     {"rules": verdict.rules})
            return RunResult("blocked", "input guard", block_reason="PERMISSION_DENIED",
                             detail=f"The task text matched the input guard ({verdict.describe()}).")
        run_tier = 1 if tier == 3 else tier                      # tier 3: draft only, then ask the Founder
        workspace = self.home / employee["workspace"]
        self.ensure_workspace(workspace)
        rules = self._policy_for(workspace, run_tier, employee)
        pack = context.build(self.home, task, employee, ctx.inputs, run_tier, rules.web, ctx.skill_filter)
        if pack.quarantined:
            ctx.emit("guard.quarantined", f"Removed hostile text from earlier results: {'; '.join(pack.quarantined)}",
                     {"items": pack.quarantined})
        model = ctx.limits.get("model") or choose_model(task, employee)
        token = uuid.uuid4().hex[:12]
        max_turns = int(ctx.limits.get("max_turns") or self.cfg.max_turns)
        run_timeout = float(ctx.limits.get("timeout") or self.cfg.timeout)
        ctx.record_prompt(model, len(pack.prompt) + len(pack.rules), context.naive_size(self.home, task, employee, ctx.inputs, ctx.history_events))
        command, policy_file, settings_file = self.build_command(ctx, pack, run_tier, rules, model, token, max_turns, run_timeout)
        ctx.emit("task.agent_started", f"Agent started ({model}, tier {run_tier}, {'worker user' if self.cfg.worker_user else 'same user'})",
                 {"model": model, "tier": run_tier, "skills": pack.skills_used, "prompt_chars": len(pack.prompt) + len(pack.rules)})
        before = self._snapshot(workspace)
        state = StreamState()
        log_path = self._log_path(task)
        stderr_tail: list[str] = []
        started = time.monotonic()
        try:
            proc = subprocess.Popen(command, cwd=workspace, env=clean_env(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                    start_new_session=True)
        except OSError as err:
            return RunResult("failed", f"could not start the worker: {err}", detail=self.redactor.scrub(str(err)))
        readers = [threading.Thread(target=self._read_stdout, args=(proc, ctx, state, log_path), daemon=True),
                   threading.Thread(target=self._read_stderr, args=(proc, stderr_tail), daemon=True)]
        for reader in readers:
            reader.start()
        cancelled = timed_out = False
        try:
            while proc.poll() is None:
                if ctx.cancel.is_set():
                    cancelled = True
                    self._stop(proc, token)
                    break
                if time.monotonic() - started > run_timeout:
                    timed_out = True
                    self._stop(proc, token)
                    break
                time.sleep(0.1)
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self._stop(proc, token, force=True)
        finally:
            for reader in readers:
                reader.join(timeout=5)
            for path in (policy_file, settings_file):
                path.unlink(missing_ok=True)
        return self._result(ctx, task, workspace, before, state, proc.returncode, cancelled, timed_out,
                            "".join(stderr_tail), model, tier)

    # ------------------------------------------------------------ process handling

    def _stop(self, proc: subprocess.Popen, token: str, force: bool = False) -> None:
        try:
            if not force:
                proc.terminate()                       # sudo and timeout pass SIGTERM on to Claude Code
                try:
                    proc.wait(timeout=10)
                    return
                except subprocess.TimeoutExpired:
                    pass
            proc.kill()
        except OSError:
            pass
        if self.cfg.worker_user:                       # the worker's processes are not ours to signal directly
            subprocess.run(self._sudo("pkill", "-KILL", "-f", f"{token}.settings.json"), capture_output=True)

    def _log_path(self, task: dict) -> Path:
        folder = self.home / "memory" / "run-logs"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{task['task_id']}-attempt{task.get('attempts', 0)}.jsonl"

    def _read_stderr(self, proc: subprocess.Popen, tail: list[str]) -> None:
        for line in proc.stderr:
            tail.append(line)
            del tail[:-40]

    def _read_stdout(self, proc: subprocess.Popen, ctx: RunContext, state: StreamState, log_path: Path) -> None:
        with open(log_path, "a", encoding="utf-8") as log:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                if state.log_bytes < 2_000_000:
                    clean = self.redactor.scrub(line)
                    log.write(clean + "\n")
                    state.log_bytes += len(clean)
                try:
                    self._handle(json.loads(line), ctx, state)
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue                           # not JSON or unexpected shape: kept in the log only

    def _emit(self, ctx: RunContext, state: StreamState, kind: str, summary: str, payload: dict) -> None:
        if state.events_emitted < MAX_EVENTS_PER_RUN:
            state.events_emitted += 1
            ctx.emit(kind, self.redactor.scrub(summary), self.redactor.scrub_obj(payload))

    def _handle(self, message: dict, ctx: RunContext, state: StreamState) -> None:
        kind = message.get("type")
        if kind == "assistant":
            for block in message["message"]["content"]:
                if block.get("type") == "tool_use":
                    state.tool_calls += 1
                    target = tool_target(block["name"], block.get("input", {}))
                    state.pending[block["id"]] = (block["name"], target)
                    self._emit(ctx, state, "task.tool", f"{block['name']}: {target}",
                               {"tool": block["name"], "target": target})
                elif block.get("type") == "text":
                    state.last_text = block["text"]
        elif kind == "user":
            content = message["message"]["content"]
            for block in content if isinstance(content, list) else []:
                if block.get("type") != "tool_result" or not block.get("is_error"):
                    continue
                text = block.get("content")
                text = " ".join(part.get("text", "") for part in text) if isinstance(text, list) else str(text)
                if policy.SENTINEL in text:
                    tool, target = state.pending.get(block.get("tool_use_id"), ("?", ""))
                    match = re.search(r"\[([a-z_.]+)\]", text[text.index(policy.SENTINEL):])
                    rule = match.group(1) if match else "unknown"
                    state.blocked.append({"tool": tool, "target": target, "rule": rule})
                    self._emit(ctx, state, "guard.blocked", f"Blocked {tool} on '{target}' ({rule})",
                               {"tool": tool, "target": target, "rule": rule})
        elif kind == "result":
            state.result = message

    # ------------------------------------------------------------ outcome

    def _snapshot(self, workspace: Path) -> dict[str, tuple[int, int]]:
        found: dict[str, tuple[int, int]] = {}
        if workspace.is_dir():
            for path in workspace.rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink():
                        stat = path.stat()
                        found[str(path.relative_to(workspace))] = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    continue
        return found

    def _artifacts(self, workspace: Path, before: dict[str, tuple[int, int]]) -> list[dict]:
        out = []
        for relative, stat in sorted(self._snapshot(workspace).items()):
            if before.get(relative) == stat or len(out) >= MAX_ARTIFACTS:
                continue
            path = workspace / relative
            digest = ""
            if stat[0] <= MAX_ARTIFACT_BYTES:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            out.append({"path": str(path.relative_to(self.home)), "sha256": digest})
        return out

    def _result(self, ctx, task, workspace, before, state, returncode, cancelled, timed_out, stderr, model, tier) -> RunResult:
        artifacts = self._artifacts(workspace, before)
        result = state.result or {}
        usage = result.get("usage") or {}
        tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                       "cache_creation_input_tokens", "cache_read_input_tokens"))
        if tokens:
            ctx.record_usage(model, tokens)
        denials = len(state.blocked)
        if cancelled:
            return RunResult("stopped", "stopped on request", artifacts=artifacts)
        if timed_out:
            return RunResult("blocked", "run time limit reached", block_reason="TIMEOUT",
                             detail=f"The agent did not finish within {float(ctx.limits.get("timeout") or self.cfg.timeout):.0f}s.", artifacts=artifacts)
        text = self.redactor.scrub(str(result.get("result") or state.last_text or ""))
        if result.get("is_error") or (returncode not in (0, None) and not result):
            blob = f"{text} {stderr}"
            payload = self.redactor.scrub(blob)[:500]
            if USAGE_RE.search(blob):
                ctx.emit("engine.usage_limit_seen", "Claude reported a usage limit", {"raw": payload})
                return RunResult("blocked", "usage limit", block_reason="USAGE_LIMIT", detail=payload, artifacts=artifacts)
            if LOGIN_RE.search(blob):
                return RunResult("blocked", "login needed", block_reason="LOGIN_REQUIRED", detail=payload, artifacts=artifacts)
            return RunResult("failed", "the agent run failed", detail=payload, artifacts=artifacts)
        if result.get("subtype") == "error_max_turns" or result.get("terminal_reason") == "max_turns":
            return RunResult("blocked", "turn limit reached", block_reason="TIMEOUT",
                             detail=f"The agent used all {ctx.limits.get('max_turns') or self.cfg.max_turns} turns.", artifacts=artifacts)
        if not result:
            return RunResult("failed", "no result from the agent", detail=self.redactor.scrub(stderr)[-500:],
                             artifacts=artifacts)
        summary = self._summary(workspace, text)
        if denials:
            summary = f"{denials} blocked action(s) were attempted. {summary}"
        if tier >= 3:
            return RunResult("awaiting_approval", summary, artifacts=artifacts)
        if denials:
            return RunResult("review", summary, artifacts=artifacts)
        return RunResult("done", summary, artifacts=artifacts)

    def _summary(self, workspace: Path, fallback: str) -> str:
        result_file = workspace / "RESULT.md"
        try:
            if result_file.is_file() and not result_file.is_symlink():
                return self.redactor.scrub(result_file.read_text(encoding="utf-8", errors="replace"))[:400]
        except OSError:
            pass
        return fallback[:400]
