import logging
import threading
import time

from .common import XhsProviderTimeoutError
from .rate_limits import consume_fixed_window


logger = logging.getLogger(__name__)


class DouyinUltraAttemptRateLimitedError(XhsProviderTimeoutError):
    def __init__(self, retry_after=0):
        super().__init__("超高清解析请求过于频繁，请稍后再试。")
        self.retry_after = max(0, int(retry_after or 0))


class DouyinUltraAttemptAccountingError(XhsProviderTimeoutError):
    pass


class DouyinUltraAttemptSupersededError(XhsProviderTimeoutError):
    """A reusable cache result made a fresh provider call unnecessary."""


class DouyinUltraAttemptController:
    """Charges one user attempt when a race starts fresh external work."""

    preflight_wait_seconds = 1.0

    def __init__(
        self,
        *,
        client_identity,
        limit,
        consume=consume_fixed_window,
    ):
        self.client_identity = str(client_identity or "unknown")
        self.limit = max(0, int(limit or 0))
        self.consume = consume
        self._condition = threading.Condition()
        self._expected_providers = set()
        self._preflight = {}
        self._started = False
        self._denied = False
        self._accounting_error = False
        self._retry_after = 0
        self._providers = set()
        self._cooldown_retry_at = 0.0

    def configure_providers(self, provider_names):
        with self._condition:
            self._expected_providers = {
                str(name or "").strip().lower()
                for name in provider_names
                if str(name or "").strip()
            }
            self._condition.notify_all()

    def _record_preflight(self, provider, state, *, reason="", retry_after=0):
        provider = str(provider or "unknown").strip().lower() or "unknown"
        with self._condition:
            self._preflight[provider] = {
                "state": state,
                "reason": str(reason or "")[:80],
            }
            retry_after = max(0, int(retry_after or 0))
            if retry_after:
                self._cooldown_retry_at = max(
                    self._cooldown_retry_at,
                    time.time() + retry_after,
                )
            self._condition.notify_all()

    def reuse_cached_result(self, provider, *, reason="payload_cache"):
        self._record_preflight(provider, "reused", reason=reason)
        logger.info(
            "douyin_ultra_attempt_quota_cache_hit_bypass",
            extra={"provider": str(provider or "unknown")[:40], "reason": str(reason or "")[:80]},
        )

    def prepare_fresh_work(self, provider):
        self._record_preflight(
            provider,
            "fresh",
            reason="fresh_work_ready",
        )

    def skip_fresh_work(self, provider, *, reason, retry_after=0):
        self._record_preflight(
            provider,
            "no_fresh_work",
            reason=reason,
            retry_after=retry_after,
        )
        event = (
            "douyin_ultra_cooldown_hit"
            if retry_after
            else "douyin_ultra_attempt_quota_cache_hit_bypass"
        )
        logger.info(
            event,
            extra={
                "provider": str(provider or "unknown")[:40],
                "reason": str(reason or "")[:80],
                "retry_after": max(0, int(retry_after or 0)),
            },
        )

    def _preflight_complete(self):
        return bool(
            self._expected_providers
            and self._expected_providers.issubset(self._preflight)
        )

    def _has_reusable_result(self):
        return any(
            item.get("state") == "reused"
            for provider, item in self._preflight.items()
            if not self._expected_providers or provider in self._expected_providers
        )

    def begin_provider(self, provider):
        provider = str(provider or "unknown").strip().lower() or "unknown"
        with self._condition:
            if provider not in self._preflight:
                self._preflight[provider] = {
                    "state": "fresh",
                    "reason": "external_start",
                }
            self._condition.notify_all()
            wait_deadline = time.monotonic() + self.preflight_wait_seconds
            while (
                not self._preflight_complete()
                and not self._has_reusable_result()
                and time.monotonic() < wait_deadline
            ):
                self._condition.wait(
                    timeout=max(0.0, wait_deadline - time.monotonic())
                )

            if self._has_reusable_result():
                raise DouyinUltraAttemptSupersededError(
                    "已有超高清缓存结果，无需启动新的上游请求"
                )
            if self._started:
                self._providers.add(provider)
                return False
            if self._denied:
                raise DouyinUltraAttemptRateLimitedError(self._retry_after)
            if self._accounting_error:
                raise DouyinUltraAttemptAccountingError(
                    "超高清请求保护服务暂时不可用，请稍后再试。"
                )

            try:
                result = self.consume(
                    "ultra-hd-minute-ip",
                    self.client_identity,
                    self.limit,
                    60,
                )
            except Exception as error:
                self._accounting_error = True
                logger.exception(
                    "douyin_ultra_attempt_accounting_failed",
                    extra={"provider": provider},
                )
                raise DouyinUltraAttemptAccountingError(
                    "超高清请求保护服务暂时不可用，请稍后再试。"
                ) from error

            if not result.allowed:
                self._denied = True
                self._retry_after = max(0, int(result.retry_after or 0))
                logger.info(
                    "douyin_ultra_attempt_rate_limited",
                    extra={
                        "provider": provider,
                        "current": int(result.current or 0),
                        "limit": int(result.limit or 0),
                        "retry_after": self._retry_after,
                    },
                )
                raise DouyinUltraAttemptRateLimitedError(self._retry_after)

            self._started = True
            self._providers.add(provider)
            logger.info(
                "douyin_ultra_attempt_started",
                extra={
                    "provider": provider,
                    "current": int(result.current or 0),
                    "limit": int(result.limit or 0),
                },
            )
            return True

    def raise_if_blocked(self):
        with self._condition:
            if self._denied:
                raise DouyinUltraAttemptRateLimitedError(self._retry_after)
            if self._accounting_error:
                raise DouyinUltraAttemptAccountingError(
                    "超高清请求保护服务暂时不可用，请稍后再试。"
                )

    @property
    def started(self):
        with self._condition:
            return self._started

    @property
    def denied(self):
        with self._condition:
            return self._denied

    @property
    def retry_after(self):
        with self._condition:
            return self._retry_after

    @property
    def cooldown_retry_after(self):
        with self._condition:
            return max(
                0,
                int(self._cooldown_retry_at - time.time() + 0.999),
            )

    def record_cooldown(self, provider, retry_after):
        retry_after = max(0, int(retry_after or 0))
        if not retry_after:
            return
        with self._condition:
            self._cooldown_retry_at = max(
                self._cooldown_retry_at,
                time.time() + retry_after,
            )
            self._condition.notify_all()
        logger.info(
            "douyin_ultra_cooldown_recorded",
            extra={
                "provider": str(provider or "unknown")[:40],
                "retry_after": retry_after,
            },
        )

    @property
    def providers(self):
        with self._condition:
            return tuple(sorted(self._providers))
