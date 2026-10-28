"""Real-time scheduler for ChainRun.

It does what the contract fakes do on virtual time: start the Providers ChainRun
names, report each outcome and every deadline in time order, and stop when the
run is done. Providers still running at that point finish in the background
within their own budget; their outcomes only update cooldowns.
"""
from concurrent.futures import ThreadPoolExecutor
import contextvars
import queue
import time

from ..pipeline import ChainRun, Work, invoke

_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="unified-chain")

# Margin past the last deadline before the scheduler stops waiting on its own;
# ChainRun always reaches a deadline first, so this only guards against bugs.
_SAFETY_MARGIN_SECONDS = 2.0


def run_chain(config, adapters, source_urls, cooldowns, *, clock=time.monotonic, executor=None, results=None):
    """Run the chain; ``results`` (a dict), when given, collects each Provider's Work as it arrives."""
    run = ChainRun(config, cooldowns)
    outcomes = queue.Queue()
    executor = executor or _EXECUTOR

    def finished(name, future):
        outcome = future.result()
        if results is not None and isinstance(outcome, Work):
            results[name] = outcome
        if run.done:
            # Late outcomes still count for cooldowns (INV-02).
            run.on_outcome(name, outcome, clock())
        else:
            outcomes.put((name, outcome))

    def launch(step):
        for start in step.start:
            context = contextvars.copy_context()
            future = executor.submit(context.run, invoke, adapters[start.provider],
                                     source_urls[start.provider], start.budget_seconds)
            future.add_done_callback(lambda done, name=start.provider: finished(name, done))

    started = clock()
    hard_stop = started + max(config.budget_seconds, config.upgrade_window_seconds) + _SAFETY_MARGIN_SECONDS
    launch(run.start(started))
    while not run.done:
        deadline = run.next_deadline
        now = clock()
        if now >= hard_stop:
            break
        wait = (hard_stop if deadline is None else min(deadline, hard_stop)) - now
        try:
            name, outcome = outcomes.get(timeout=max(0.0, wait))
        except queue.Empty:
            if deadline is not None and clock() >= deadline:
                launch(run.on_time(clock()))
            continue
        now = clock()
        if deadline is not None and now >= deadline:
            # The deadline passed while this outcome waited; report it first.
            launch(run.on_time(now))
            if run.done:
                break
        launch(run.on_outcome(name, outcome, now))
    while True:
        try:
            name, outcome = outcomes.get_nowait()
        except queue.Empty:
            return run
        run.on_outcome(name, outcome, clock())
