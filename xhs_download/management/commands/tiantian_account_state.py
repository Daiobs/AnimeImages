"""Local account-guard inspection/recovery, never sends or replays a request."""
import json

from django.core.management.base import BaseCommand, CommandError

from xhs_download.services.platforms.tiantian_serial import inspect_or_recover


class Command(BaseCommand):
    help = "Inspect TianTian account guard or record evidenced recovery for future requests."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--expected-state")
        parser.add_argument("--evidence", choices=("remote_terminal_evidence", "human_risk_acceptance"))
        parser.add_argument("--reference")

    def handle(self, *args, **options):
        if bool(options['expected_state']) != bool(options['evidence'] and options['reference']):
            raise CommandError("Recovery requires expected state, evidence and reference together")
        try:
            result = inspect_or_recover(expected_state=options['expected_state'],
                                        evidence=options['evidence'], reference=options['reference'])
        except (ValueError, RuntimeError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(json.dumps(result))
