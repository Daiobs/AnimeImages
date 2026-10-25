"""Staff link-only authorization uses the existing route without download events."""

from hashlib import sha256
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from . import views
from .models import DownloadAuditEvent, DownloadRequestLog, ObservabilityStatus, RoutingDecisionLog
from .services.provider_manifest import build_provider_manifest_token
from .services.upstream_delivery import normalize_upstream_result


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class UpstreamResourceLinkTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.staff = get_user_model().objects.create_user(username="resource-link-staff", is_staff=True)
        self.factory = RequestFactory()
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("No network permitted"))
        self.network = network.start()
        self.addCleanup(network.stop)
        self.item = self.issue_item()

    def issue_item(self):
        with patch("xhs_download.services.upstream_delivery.validate_public_download_url", side_effect=lambda url: url):
            result = normalize_upstream_result(
                [[3, "https://media.example/image.png", {"extension": "png"}]],
                match={"provider_id": "parsehub_generic", "extractor_id": "TieBaParser",
                       "label": "Tieba", "version": "test"},
                source_url="https://tieba.baidu.com/p/123",
            )
        return result.assets[0].to_download_item()

    def cache_key(self, item):
        return "upstream:asset:v1:" + sha256(item["asset_id"].encode()).hexdigest()

    def post(self, *, user=None, **changes):
        payload = {"intent": "resource_link", "route_only": True,
                   "media_items": [self.item], "save_format": "original"}
        payload.update(changes)
        request = self.factory.post("/download/", json.dumps(payload), content_type="application/json")
        request.user = self.staff if user is None else user
        request._dont_enforce_csrf_checks = True
        response = views.download_images_api(request)
        self.network.assert_not_called()
        return response

    def test_open_and_copy_authorizations_do_not_create_download_logs(self):
        before = DownloadRequestLog.objects.count()
        for action in ("open", "copy"):
            with self.subTest(action=action), patch.object(
                views, "validate_download_media_items", wraps=views.validate_download_media_items,
            ) as validate, patch.object(
                views, "check_download_cost_preflight", wraps=views.check_download_cost_preflight,
            ) as preflight:
                response = self.post()
                self.assertEqual(response.status_code, 200, response.content)
                route = json.loads(response.content)
                self.assertEqual(route["route_type"], "direct")
                self.assertEqual(len(route["items"]), 1)
                bound = route["items"][0]
                for key in ("asset_id", "url", "type", "provider", "provider_resource_id",
                            "provider_cache_key", "source_platform"):
                    self.assertEqual(bound[key], self.item[key])
                self.assertNotIn("provider_manifest_token", bound)
                self.assertIs(bound["browser_upstream_bound"], True)
                self.assertEqual(response["Cache-Control"], "private, no-store")
                validate.assert_called_once()
                preflight.assert_called_once()
                self.assertEqual(DownloadRequestLog.objects.count(), before)

    def test_ordinary_route_records_preparation_and_dispatch_not_transfer_success(self):
        before = DownloadRequestLog.objects.count()
        response = self.post(intent=None)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(DownloadRequestLog.objects.count(), before + 1)
        log = DownloadAuditEvent.objects.latest("pk")
        self.assertEqual((log.phase, log.status), ("prepared", "pending"))
        self.assertEqual(log.actual_bytes, 0)
        self.assertEqual(log.route_type, "direct")
        self.assertFalse(DownloadAuditEvent.objects.filter(phase="transfer_completed").exists())
        self.assertEqual(DownloadRequestLog.objects.get().status, ObservabilityStatus.SUCCESS)
        self.assertTrue(RoutingDecisionLog.objects.get().success)

    def test_mature_provider_route_is_prepared_and_link_intent_remains_denied(self):
        fields = {"asset_id": "douyin:123456789012345678:video:tiantianjiexi",
                  "provider": "tiantianjiexi", "provider_resource_id": "123456789012345678",
                  "provider_cache_key": "douyin:tiantianjiexi:validated:123456789012345678",
                  "url": "https://v3-dy-o.zjcdn.com/path/video.mp4"}
        item = {**fields, "type": "video", "provider_manifest_token":
                build_provider_manifest_token(**fields, media_type="video")}
        self.assertEqual(self.post(media_items=[item]).status_code, 403)
        self.assertEqual(DownloadRequestLog.objects.count(), 0)
        response = self.post(intent=None, media_items=[item])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(DownloadRequestLog.objects.count(), 1)
        log = DownloadAuditEvent.objects.get()
        self.assertEqual((log.phase, log.status), ("prepared", "pending"))
        self.assertEqual(log.route_type, "direct")
        self.assertTrue(RoutingDecisionLog.objects.get().success)

    def test_anonymous_nonstaff_and_inactive_staff_are_denied(self):
        ordinary = get_user_model().objects.create_user(username="resource-link-user")
        inactive = get_user_model().objects.create_user(
            username="resource-link-inactive", is_staff=True, is_active=False,
        )
        for user in (AnonymousUser(), ordinary, inactive):
            with self.subTest(user=str(user)):
                response = self.post(user=user)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(json.loads(response.content)["error_code"], "RESOURCE_LINK_FORBIDDEN")
        self.assertEqual(DownloadRequestLog.objects.count(), 0)

    def test_link_intent_is_single_upstream_image_route_only(self):
        cases = [
            {"route_only": False}, {"route_only": "true"},
            {"download_mode": "zip"}, {"download_mode": "server"}, {"download_mode": []},
            {"media_items": []}, {"media_items": [self.item, self.item]},
            {"media_items": [None]}, {"media_items": "invalid"},
            {"media_items": [{**self.item, "type": "video"}]},
            {"media_items": [{"type": "image", "url": self.item["url"]}]},
            {"media_items": [], "image_urls": [self.item["url"]]},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                response = self.post(**fields)
                self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(DownloadRequestLog.objects.count(), 0)

    def test_tampered_resource_and_signature_are_denied(self):
        for key, value in (
            ("url", "https://media.example/other.png"), ("asset_id", "upstream:forged:image:1"),
            ("provider_manifest_token", self.item["provider_manifest_token"] + "tampered"),
            ("provider_cache_key", "upstream:another-job"), ("source_platform", "other"),
        ):
            with self.subTest(field=key):
                response = self.post(media_items=[{**self.item, key: value, "browser_upstream_bound": True}])
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(value, response.content.decode())
        self.assertEqual(DownloadRequestLog.objects.count(), 0)

    @override_settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=60)
    def test_clock_expiry_rejects_even_when_cache_is_current(self):
        with patch("django.core.signing.time") as clock:
            clock.time.return_value = 1700000000
            self.item = self.issue_item()
            record = cache.get(self.cache_key(self.item))
            clock.time.return_value = 1700000060
            self.assertEqual(self.post().status_code, 200)
            clock.time.return_value = 1700000061
            self.assertEqual(cache.get(self.cache_key(self.item)), record)
            self.assertEqual(self.post().status_code, 403)
        self.assertEqual(DownloadRequestLog.objects.count(), 0)

    def test_revoked_resource_is_denied(self):
        cache.delete(self.cache_key(self.item))
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(DownloadRequestLog.objects.count(), 0)

    def test_revocation_during_admission_is_rechecked(self):
        def revoke(*args):
            cache.delete(self.cache_key(self.item))
            return None

        with patch.object(views, "check_download_cost_preflight", side_effect=revoke):
            self.assertEqual(self.post().status_code, 403)
        self.assertEqual(DownloadRequestLog.objects.count(), 0)

    def test_route_substitution_or_fallback_cannot_grant_link(self):
        for change in ("url", "asset_id", "binding", "fallback"):
            def change_route(request, items, route, **kwargs):
                if change == "fallback":
                    return {**route, "route_type": "zip"}
                item = dict(route["items"][0])
                item[{"binding": "browser_upstream_bound"}.get(change, change)] = False if change == "binding" else "forged"
                return {**route, "items": [item]}

            with self.subTest(change=change), patch.object(
                views, "apply_policy_decision_to_route", side_effect=change_route,
            ):
                self.assertEqual(self.post().status_code, 403)
        self.assertEqual(DownloadRequestLog.objects.count(), 0)

    def test_link_intent_does_not_bypass_cost_admission(self):
        with patch.object(views, "check_download_cost_preflight", return_value=(
            "SINGLE_ARCHIVE_SIZE_LIMIT", "Test limit", 413,
        )):
            response = self.post()
        self.assertEqual(response.status_code, 413)
        self.assertFalse(DownloadRequestLog.objects.filter(status=ObservabilityStatus.SUCCESS).exists())
