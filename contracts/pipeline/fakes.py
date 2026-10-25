"""Fake Providers and a virtual-time scheduler for the pipeline contracts.

The fakes return unified results or failures directly; no platform code,
network or real clock is involved. The scheduler is the minimum a real one
must do: start what ChainRun names, report outcomes and deadlines in time
order, and stop when it is done. An outcome due exactly at a deadline is
reported first.
"""
from dataclasses import dataclass, field
import heapq
import itertools

from xhs_download.services.pipeline import (
    ChainRun,
    CooldownBook,
    Failure,
    FailureKind,
    Fetchable,
    Item,
    Kind,
    Tracks,
    Variant,
    Work,
    invoke,
)

SOURCE = "https://example.test/works/contract-1"


@dataclass
class FakeProvider:
    latency: float
    outcome: object
    calls: list = field(default_factory=list)

    def parse(self, source_url, budget):
        self.calls.append((source_url, budget))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def failure(kind, code="FAKE_FAILURE"):
    return Failure(FailureKind(kind), code)


def variant(variant_id="v1", *, height=None, tracks=Tracks.AV, container="mp4",
            fetchable=Fetchable.TRUE, **facts):
    return Variant(variant_id=variant_id, url=f"https://media.example.test/{variant_id}",
                   container=container, tracks=tracks, height=height,
                   browser_fetchable=fetchable, **facts)


def video_work(height=1080, *, work_id="contract-1", title="契约作品", variants=None, **fields):
    variants = variants or (variant(f"h{height}", height=height),)
    item = Item(item_id="i1", kind=Kind.VIDEO, index=1, variants=tuple(variants))
    return Work(source_url=SOURCE, work_id=work_id, title=title, items=(item,), **fields)


@dataclass
class Timeline:
    events: list = field(default_factory=list)

    def started(self):
        return [event[2] for event in self.events if event[1] == "start"]

    def shows(self):
        return [(event[0], event[2], event[3]) for event in self.events if event[1] == "show"]

    def budgets(self):
        return {event[2]: event[3] for event in self.events if event[1] == "start"}


def simulate(config, providers, cooldowns=None, *, start_at=0.0):
    """Run one chain to completion on virtual time; return (run, timeline)."""
    cooldowns = cooldowns or CooldownBook(base_seconds=30, max_seconds=300)
    run = ChainRun(config, cooldowns)
    timeline = Timeline()
    queue = []
    order = itertools.count()

    def apply(step, now):
        for start in step.start:
            timeline.events.append((now, "start", start.provider, start.budget_seconds))
            provider = providers[start.provider]
            outcome = invoke(provider, SOURCE, start.budget_seconds)
            finish = now + provider.latency
            if provider.latency > start.budget_seconds:
                # Adapters must answer within their budget (§2.1).
                outcome = Failure(FailureKind.TIMEOUT, "FAKE_BUDGET_TIMEOUT")
                finish = now + start.budget_seconds
            heapq.heappush(queue, (finish, next(order), start.provider, outcome))
        if step.show is not None:
            timeline.events.append((now, "show", step.show.provider, step.show.completeness.value))
        if step.done:
            timeline.events.append((now, "done", step.failure))

    now = start_at
    apply(run.start(now), now)
    while not run.done:
        deadline = run.next_deadline
        if queue and (deadline is None or queue[0][0] <= deadline):
            now, _, name, outcome = heapq.heappop(queue)
            apply(run.on_outcome(name, outcome, now), now)
        elif deadline is not None:
            now = deadline
            apply(run.on_time(now), now)
        else:
            break
    return run, timeline
