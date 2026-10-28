"""Runner interface and the simulated runner used for tests and demos.

A runner does the actual work for one task (later: Claude Code, a browser). The engine owns
scheduling, leases, retries and logging; a runner only returns a :class:`RunResult`.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

OUTCOMES = ("done", "review", "awaiting_approval", "blocked", "failed", "stopped")


@dataclass
class RunResult:
    outcome: str
    summary: str = ""
    block_reason: str | None = None
    detail: str | None = None
    checkpoint: str | None = None
    artifacts: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}")


@dataclass
class RunContext:
    task: dict
    employee: dict
    attempt: int
    checkpoint: str | None
    cancel: threading.Event
    progress: Callable[[int, str], None]      # progress(percent, message) -> logged event
    save_checkpoint: Callable[[str], None]    # remember where to resume after a crash or stop
    inputs: list[dict] = field(default_factory=list)                        # results of finished dependencies
    emit: Callable[..., None] = lambda kind, summary, payload=None: None    # write an audit event as the employee
    record_usage: Callable[[str, int], None] = lambda model, tokens: None   # note token use for tuning
    record_prompt: Callable[[str, int, int], None] = lambda model, sent, baseline: None   # characters sent vs the naive baseline
    skill_filter: Callable[[str], bool] | None = None                       # registry: may this employee's agent see this skill?
    limits: dict = field(default_factory=dict)                              # model / max_turns / timeout for this run
    history_events: int = 0                                                 # size of the task's history (for the naive baseline)


class Runner(Protocol):
    name: str
    simulated: bool

    def run(self, ctx: RunContext) -> RunResult: ...


class FakeRunner:
    """Simulated work. Behaviour can be scripted with markers in the task title:

    ``[fail]`` always fails · ``[fail-once]`` fails on the first attempt · ``[block:CAPTCHA]`` blocks ·
    ``[crash]`` raises · ``[review]`` / ``[approval]`` end in that state · ``[hang]`` ignores cancel ·
    ``[quick]`` finishes in ~0.2 s.
    """

    name = "fake"
    simulated = True

    def __init__(self, duration: tuple[float, float] = (0.05, 0.15), seed: int | None = 7,
                 hang_seconds: float = 30.0) -> None:
        self.duration = duration
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self.hang_seconds = hang_seconds
        self.started: list[tuple[str, float]] = []
        self.finished: list[tuple[str, float]] = []
        self._concurrent = 0
        self.max_concurrent = 0

    def _sleep(self, seconds: float, ctx: RunContext, honour_cancel: bool = True) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if honour_cancel and ctx.cancel.is_set():
                return False
            time.sleep(min(0.02, max(end - time.monotonic(), 0)))
        return True

    def run(self, ctx: RunContext) -> RunResult:
        title = ctx.task["title"]
        with self._lock:
            self._concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self._concurrent)
            self.started.append((ctx.task["task_id"], time.monotonic()))
            seconds = 0.2 if "[quick]" in title else self._rng.uniform(*self.duration)
        try:
            if "[hang]" in title:
                self._sleep(self.hang_seconds, ctx, honour_cancel=False)
            if "[crash]" in title:
                raise RuntimeError("simulated crash")
            ctx.progress(10, "started")
            if not self._sleep(seconds / 2, ctx):
                return RunResult("stopped", "stopped midway", checkpoint="half")
            ctx.save_checkpoint("half")
            ctx.progress(50, "halfway")
            if not self._sleep(seconds / 2, ctx):
                return RunResult("stopped", "stopped midway", checkpoint="half")
            if "[fail]" in title or ("[fail-once]" in title and ctx.attempt == 1):
                return RunResult("failed", "simulated failure", detail="simulated failure")
            marker = title.split("[block:")
            if len(marker) > 1:
                return RunResult("blocked", "needs a human", block_reason=marker[1].split("]")[0],
                                 detail="simulated block")
            if "[review]" in title:
                return RunResult("review", "ready for review")
            if "[approval]" in title:
                return RunResult("awaiting_approval", "needs Founder approval")
            return RunResult("done", f"simulated result for {title}")
        finally:
            with self._lock:
                self._concurrent -= 1
                self.finished.append((ctx.task["task_id"], time.monotonic()))
