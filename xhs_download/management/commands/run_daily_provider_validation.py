import json

from django.core.management.base import BaseCommand, CommandError

from xhs_download.models import (
    ProviderValidationNotificationStatus,
    ProviderValidationSuiteStatus,
    ProviderValidationSuiteTrigger,
)
from xhs_download.services.provider_daily_validation import (
    DailyValidationConfigurationError,
    run_daily_provider_validation,
)
from xhs_download.services.provider_health_tests import provider_health_test_keys


class Command(BaseCommand):
    help = "Run the bounded daily Provider validation suite."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--no-email", action="store_true")
        parser.add_argument(
            "--test-key",
            action="append",
            dest="test_keys",
            choices=provider_health_test_keys(),
        )
        parser.add_argument("--fail-on-degraded", action="store_true")

    def handle(self, *args, **options):
        del args
        trigger = (
            ProviderValidationSuiteTrigger.COMMAND
            if options["force"] or options["dry_run"] or options.get("test_keys")
            else ProviderValidationSuiteTrigger.SCHEDULED
        )
        try:
            outcome = run_daily_provider_validation(
                force=options["force"],
                no_email=options["no_email"],
                test_keys_override=options.get("test_keys"),
                dry_run=options["dry_run"],
                trigger=trigger,
            )
        except DailyValidationConfigurationError as error:
            payload = {"status": "failed", "error_code": str(error)[:80]}
            raise CommandError(
                json.dumps(payload, sort_keys=True),
                returncode=2,
            ) from None
        except Exception:
            raise CommandError(
                json.dumps(
                    {"status": "failed", "error_code": "SUITE_ORCHESTRATION_FAILED"},
                    sort_keys=True,
                ),
                returncode=2,
            ) from None

        payload = outcome.as_safe_dict()
        output = json.dumps(payload, sort_keys=True)
        suite = outcome.suite
        if (
            suite is not None
            and suite.notification_status
            == ProviderValidationNotificationStatus.FAILED
        ):
            raise CommandError(output, returncode=3)
        if (
            options["fail_on_degraded"]
            and suite is not None
            and suite.status == ProviderValidationSuiteStatus.DEGRADED
        ):
            raise CommandError(output, returncode=4)
        return output
