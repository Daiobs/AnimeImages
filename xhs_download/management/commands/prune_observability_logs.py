from django.core.management.base import BaseCommand
from django.utils import timezone

from ...models import DownloadRequestLog, ParseRequestLog


class Command(BaseCommand):
    help = "Prune old parse/download observability logs. Provider audit logs are retained."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        days = max(1, int(options["days"] or 30))
        cutoff = timezone.now() - timezone.timedelta(days=days)
        dry_run = bool(options["dry_run"])
        models = (
            ("parse", ParseRequestLog),
            ("download", DownloadRequestLog),
        )
        totals = {}
        for label, model in models:
            queryset = model.objects.filter(created_at__lt=cutoff)
            count = queryset.count()
            totals[label] = count
            if not dry_run:
                queryset.delete()
        payload = {
            "dry_run": dry_run,
            "days": days,
            "cutoff": cutoff.isoformat(),
            "deleted": totals if not dry_run else {"parse": 0, "download": 0},
            "matched": totals,
            "provider_audit_retained": True,
        }
        self.stdout.write(str(payload))
