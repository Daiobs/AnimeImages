import json

from django.core.management.base import BaseCommand

from ...services.policy_learning import (
    build_policy_learning_result,
    create_policy_learning_run,
    serialize_policy_learning_result,
)


class Command(BaseCommand):
    help = "Run shadow-only Policy Learning and optionally persist a PolicyLearningRun."

    def add_arguments(self, parser):
        parser.add_argument(
            "--window-hours",
            type=int,
            default=24,
            help="Learning window in hours. Defaults to 24.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the learning result without writing PolicyLearningRun.",
        )

    def handle(self, *args, **options):
        window_hours = options["window_hours"]
        if options["dry_run"]:
            result = serialize_policy_learning_result(
                build_policy_learning_result(window_hours)
            )
            self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
            return

        run = create_policy_learning_run(window_hours)
        self.stdout.write(
            json.dumps(
                {
                    "id": run.id,
                    "status": run.status,
                    "window_hours": run.window_hours,
                    "sample_count": run.sample_count,
                    "confidence": run.confidence,
                    "estimated_savings_bytes": run.estimated_savings_bytes,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
