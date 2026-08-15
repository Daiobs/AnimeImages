import io
import logging
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from djangoProject.logging import (
    PROVIDER_TERMINAL_LOG_FIELDS,
    RequestContextFormatter,
)
from xhs_download.services import provider_reliability
from xhs_download.services.provider_reliability import (
    ProviderFailureClass,
    ProviderOutcome,
    record_provider_attempt,
)


class ProviderTerminalLoggingOutputTests(SimpleTestCase):
    maxDiff = None

    def build_output_logger(self):
        formatter_config = settings.LOGGING["formatters"]["request_context"]
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(
            RequestContextFormatter(formatter_config["format"])
        )
        logger = logging.Logger("tests.provider_terminal_output", logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
        return logger, stream

    def render_provider_attempt(self, **overrides):
        logger, stream = self.build_output_logger()
        fields = {
            "provider": "tiantianjiexi",
            "outcome": ProviderOutcome.SUCCESS_FRESH,
            "external_call": True,
            "cache_hit": False,
            "cache_bypassed": False,
            "single_flight_wait": False,
            "continuation": False,
            "attempt_consumed": True,
            "elapsed_ms": 2500,
            "payload_age_ms": 0,
            "retry_after_seconds": 0,
            "candidate_count": 1,
            "media_host": "v26-default.365yg.com",
            "expected_bytes": 93733891,
            "failure_class": ProviderFailureClass.NOT_APPLICABLE,
        }
        fields.update(overrides)
        with patch.object(provider_reliability, "logger", logger):
            returned_fields = record_provider_attempt(**fields)
        return stream.getvalue(), returned_fields

    def test_success_fresh_fields_reach_final_console_line(self):
        rendered, _fields = self.render_provider_attempt()

        self.assertIn("event=provider_attempt_completed", rendered)
        self.assertIn("provider=tiantianjiexi", rendered)
        self.assertIn("outcome=SUCCESS_FRESH", rendered)
        self.assertIn("external_call=True", rendered)
        self.assertIn("cache_hit=False", rendered)
        self.assertIn("attempt_consumed=True", rendered)
        self.assertIn("elapsed_ms=2500", rendered)
        self.assertIn("payload_age_ms=0", rendered)
        self.assertIn("candidate_count=1", rendered)
        self.assertIn("media_host=v26-default.365yg.com", rendered)
        self.assertIn("expected_bytes=93733891", rendered)
        self.assertIn("failure_class=NOT_APPLICABLE", rendered)

    def test_provider_fields_have_deterministic_output_order(self):
        rendered, _fields = self.render_provider_attempt()

        positions = [rendered.index(f"{field}=") for field in PROVIDER_TERMINAL_LOG_FIELDS]
        self.assertEqual(positions, sorted(positions))

    def test_success_payload_cache_is_observable_in_final_line(self):
        rendered, _fields = self.render_provider_attempt(
            outcome=ProviderOutcome.SUCCESS_PAYLOAD_CACHE,
            external_call=False,
            cache_hit=True,
            attempt_consumed=False,
            payload_age_ms=4200,
        )

        self.assertIn("outcome=SUCCESS_PAYLOAD_CACHE", rendered)
        self.assertIn("external_call=False", rendered)
        self.assertIn("cache_hit=True", rendered)
        self.assertIn("attempt_consumed=False", rendered)
        self.assertIn("payload_age_ms=4200", rendered)

    def test_failure_cooldown_is_observable_in_final_line(self):
        rendered, _fields = self.render_provider_attempt(
            outcome=ProviderOutcome.FAILURE_COOLDOWN,
            external_call=False,
            attempt_consumed=False,
            retry_after_seconds=8,
            candidate_count=0,
            expected_bytes=0,
            failure_class=ProviderFailureClass.NOT_APPLICABLE,
        )

        self.assertIn("outcome=FAILURE_COOLDOWN", rendered)
        self.assertIn("external_call=False", rendered)
        self.assertIn("attempt_consumed=False", rendered)
        self.assertIn("retry_after_seconds=8", rendered)
        self.assertIn("failure_class=NOT_APPLICABLE", rendered)

    def test_single_flight_wait_is_observable_in_final_line(self):
        rendered, _fields = self.render_provider_attempt(
            outcome=ProviderOutcome.SUCCESS_SINGLE_FLIGHT,
            external_call=False,
            single_flight_wait=True,
            attempt_consumed=False,
        )

        self.assertIn("outcome=SUCCESS_SINGLE_FLIGHT", rendered)
        self.assertIn("single_flight_wait=True", rendered)
        self.assertIn("external_call=False", rendered)
        self.assertIn("attempt_consumed=False", rendered)

    def test_continuation_is_observable_in_final_line(self):
        rendered, _fields = self.render_provider_attempt(
            outcome=ProviderOutcome.SUCCESS_CONTINUATION,
            continuation=True,
        )

        self.assertIn("outcome=SUCCESS_CONTINUATION", rendered)
        self.assertIn("continuation=True", rendered)

    def test_generic_log_keeps_existing_format_without_provider_suffix(self):
        logger, stream = self.build_output_logger()

        logger.info("ordinary_event")
        rendered = stream.getvalue()

        self.assertIn("event=ordinary_event", rendered)
        self.assertIn("provider=", rendered)
        self.assertIn("status=", rendered)
        self.assertIn("message=ordinary_event", rendered)
        self.assertNotIn("outcome=", rendered)
        self.assertNotIn("attempt_consumed=", rendered)

    def test_formatter_contract_covers_every_provider_terminal_field(self):
        _rendered, returned_fields = self.render_provider_attempt()

        diagnostic_fields = set(returned_fields) - {"event", "provider"}
        self.assertEqual(diagnostic_fields, set(PROVIDER_TERMINAL_LOG_FIELDS))

    def test_media_url_is_reduced_to_hostname_in_final_line(self):
        rendered, _fields = self.render_provider_attempt(
            media_host=(
                "https://v26-default.365yg.com/private/video.mp4"
                "?token=super-secret"
            )
        )

        self.assertIn("media_host=v26-default.365yg.com", rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn("/private/video.mp4", rendered)
        self.assertNotIn("?", rendered)
        self.assertNotIn("token", rendered)
        self.assertNotIn("super-secret", rendered)

    def test_unknown_extra_is_not_rendered(self):
        logger, stream = self.build_output_logger()
        extra = {
            "event": "provider_attempt_completed",
            "provider": "tiantianjiexi",
            "secret_test_value": "DO_NOT_RENDER",
        }

        logger.info("provider_attempt_completed", extra=extra)
        rendered = stream.getvalue()

        self.assertNotIn("secret_test_value", rendered)
        self.assertNotIn("DO_NOT_RENDER", rendered)
        for field in PROVIDER_TERMINAL_LOG_FIELDS:
            self.assertIn(f"{field}=", rendered)

    def test_provider_terminal_line_does_not_add_raw_exception_fields(self):
        rendered, _fields = self.render_provider_attempt()

        self.assertNotIn("Traceback", rendered)
        self.assertNotIn("exception=", rendered)
        self.assertNotIn("stack=", rendered)
        self.assertNotIn("response_body=", rendered)

    def test_production_logging_settings_use_request_context_formatter(self):
        logging_config = settings.LOGGING
        formatter_config = logging_config["formatters"]["request_context"]
        console_config = logging_config["handlers"]["console"]

        self.assertEqual(console_config["class"], "logging.StreamHandler")
        self.assertEqual(console_config["formatter"], "request_context")
        self.assertEqual(
            formatter_config["()"],
            "djangoProject.logging.RequestContextFormatter",
        )
