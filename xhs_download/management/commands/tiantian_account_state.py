"""Inspect local serialization and historical state without sending requests."""
import json

from django.core.management.base import BaseCommand, CommandError

from xhs_download.services.platforms.tiantian_serial import inspect_account_state


class Command(BaseCommand):
    help = "Inspect TianTian local serialization; old outcome records do not block requests."
    requires_system_checks = []

    def handle(self, *args, **options):
        try:
            result = inspect_account_state()
        except (ValueError, RuntimeError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(json.dumps(result))
