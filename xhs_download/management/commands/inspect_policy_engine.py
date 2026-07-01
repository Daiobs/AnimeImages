import json

from django.core.management.base import BaseCommand

from ...policy.adaptive import build_adaptive_weight_recommendation
from ...policy.learning import (
    STANDARD_WINDOWS,
    build_policy_learning_snapshot,
    record_policy_learning_run,
)


class Command(BaseCommand):
    help = "Inspect Policy Engine shadow learning and adaptive weight recommendations."

    def add_arguments(self, parser):
        parser.add_argument("--window-hours", type=int, default=24)
        parser.add_argument("--all-windows", action="store_true")
        parser.add_argument("--record", action="store_true")
        parser.add_argument("--adaptive", action="store_true")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        windows = STANDARD_WINDOWS if options["all_windows"] else (options["window_hours"],)
        payload = []
        for window_hours in windows:
            if options["record"]:
                run = record_policy_learning_run(window_hours=window_hours)
                snapshot = {
                    "id": run.pk,
                    "window_hours": run.window_hours,
                    "shadow_recommendation": run.shadow_recommendation,
                    "confidence": run.confidence,
                    "estimated_savings_bytes": run.estimated_savings_bytes,
                    "sample_count": run.sample_count,
                }
            else:
                snapshot = build_policy_learning_snapshot(window_hours=window_hours)
            if options["adaptive"]:
                snapshot["adaptive_weights"] = build_adaptive_weight_recommendation(
                    window_hours=window_hours
                )
            payload.append(snapshot)

        if options["json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, default=str, indent=2))
            return

        for item in payload:
            recommendation = item.get("shadow_recommendation") or {}
            self.stdout.write(
                "window={window}h route={route} confidence={confidence} "
                "samples={samples} savings={savings}".format(
                    window=item.get("window_hours"),
                    route=recommendation.get("route") or "-",
                    confidence=item.get("confidence"),
                    samples=item.get("sample_count"),
                    savings=item.get("estimated_savings_bytes"),
                )
            )
