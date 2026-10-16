"""Exact-host configuration uses the isolated test database and no providers."""

from contextlib import ExitStack
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import UpstreamRoute, UpstreamRouteAudit
from .services.upstream_routes import read_route, source_scope, write_route


HOST = "www.douban.com"
SOURCE = "https://www.douban.com/group/topic/123/"


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ExactHostRouteTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="host-route-admin", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.endpoint = reverse("upstream_routes")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.no_calls = []
        for target in (
            "socket.getaddrinfo", "socket.gethostbyname", "socket.create_connection",
            "xhs_download.services.quality_execution.execute_provider",
            "xhs_download.services.quality_execution.match_saved_videodl",
            "xhs_download.services.quality_execution.provider_available",
            "xhs_download.services.provider_lab.runtime.execute_provider_worker",
            "xhs_download.services.upstream_matching.match_upstream_url",
            "xhs_download.quality_route_views.videodl_route_availability",
        ):
            self.no_calls.append(self.stack.enter_context(patch(
                target, side_effect=AssertionError("Host configuration must not probe: " + target))))

    def tearDown(self):
        for mock in self.no_calls:
            mock.assert_not_called()
        super().tearDown()

    def save(self, **overrides):
        payload = {"source_host": HOST, "expected_revision": 0,
                   "provider_order": ["parsehub_generic"], **overrides}
        return self.client.post(self.endpoint, json.dumps(payload), content_type="application/json")

    def reset(self, revision, host=HOST):
        return self.client.delete(self.endpoint, json.dumps({
            "source_host": host, "expected_revision": revision}), content_type="application/json")

    def test_host_read_without_work_is_readonly_and_precise(self):
        response = self.client.get(self.endpoint, {"source_host": HOST})
        self.assertEqual(response.status_code, 200)
        route = response.json()["route"]
        self.assertEqual((route["source_host"], route["revision"], route["mode"]), (HOST, 0, "default"))
        self.assertEqual(route["scope_type"], "exact_host")
        self.assertEqual(route["source_kind"], "host")
        self.assertEqual(route["limitations"], ["content_type_routing_not_supported",
            "work_reference_not_provided", "provider_availability_not_checked"])
        self.assertNotIn("source_url", route)
        self.assertFalse(UpstreamRoute.objects.exists())
        self.assertFalse(UpstreamRouteAudit.objects.exists())

    def test_save_url_interoperability_conflict_reset_and_audit(self):
        response = self.save(format_preferences={"max_height": 2160})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["route"]["revision"], 1)
        self.assertEqual(read_route(SOURCE), read_route(source_host=HOST))
        self.assertEqual(read_route(SOURCE)["format_preferences"], {"max_height": 2160})
        for host in ("douban.com", "other.douban.com", "www.coolapk.com"):
            self.assertEqual(read_route(source_host=host)["revision"], 0)
        self.assertEqual(self.save().status_code, 409)
        self.assertEqual(self.reset(0).status_code, 409)
        self.assertEqual(UpstreamRouteAudit.objects.count(), 1)
        response = self.reset(1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["route"]["revision"], 2)
        self.assertEqual(response.json()["route"]["mode"], "default")
        self.assertFalse(UpstreamRoute.objects.get().active)
        self.assertEqual(UpstreamRoute.objects.get().provider_options, {})
        audit = UpstreamRouteAudit.objects.get(revision=2)
        self.assertEqual((audit.action, audit.actor_id), ("reset", self.user.pk))
        self.assertEqual(audit.before["revision"], 1)
        self.assertEqual(audit.after["revision"], 2)
        self.assertEqual(self.save(expected_revision=1).status_code, 409)
        self.assertEqual(UpstreamRouteAudit.objects.count(), 2)
        self.assertEqual(self.save(expected_revision=2).json()["route"]["revision"], 3)

    def test_conflict_on_new_host_rolls_back_initial_row(self):
        self.assertEqual(self.save(expected_revision=3).status_code, 409)
        self.assertFalse(UpstreamRoute.objects.exists())
        self.assertFalse(UpstreamRouteAudit.objects.exists())

    def test_existing_videodl_route_read_does_not_fabricate_work_or_probe(self):
        options = {"videodl_repo": {"mode": "specific_backend", "backend_id": "ZhihuVideoClient"}}
        write_route(SOURCE, expected_revision=0, actor=self.user, provider_order=["videodl_repo"],
                    provider_options=options)
        with patch("xhs_download.services.upstream_routes.validate_url",
                   side_effect=AssertionError("No invented work URL")), patch(
                   "xhs_download.services.upstream_routes.extract_url_from_text",
                   side_effect=AssertionError("No invented work URL")):
            for _ in range(2):
                response = self.client.get(self.endpoint, {"source_host": HOST})
                self.assertEqual(response.status_code, 200)
                route = response.json()["route"]
                self.assertEqual(route["provider_options"], options)
                self.assertEqual(route["provider_availability"]["videodl_repo"], {
                    "status": "unknown", "reason": "SOURCE_URL_REQUIRED"})
        self.assertEqual(UpstreamRoute.objects.get().revision, 1)
        self.assertEqual(UpstreamRouteAudit.objects.count(), 1)
        response = self.save(expected_revision=1, provider_order=["videodl_repo"], provider_options=options)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["route"]["provider_availability"]["videodl_repo"]["status"], "unknown")

    def test_only_canonical_public_dns_hosts_are_accepted(self):
        invalid = ("", None, 1, [], {}, "WWW.douban.com", "www.douban.com.", " www.douban.com",
            "https://www.douban.com", "www.douban.com/path", "www.douban.com:443", "user@douban.com",
            "douban.com?x=1", "localhost", "a.localhost", "a.local", "a.internal", "a.invalid",
            "a.test", "a.example", "a.onion", "a.home", "a.lan", "127.0.0.1", "8.8.8.8", "[::1]",
            "a..com", "-a.com", "a-.com", "a_foo.com", "a.123", "xn--.com", "a" * 64 + ".com",
            ".".join(["a" * 63] * 4), "b\u00fccher.de")
        for host in invalid:
            with self.subTest(host=host):
                response = self.save(source_host=host)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error_code"], "INVALID_SOURCE_HOST")
        self.assertEqual(self.client.get(self.endpoint, {"source_host": "xn--bcher-kva.de"}).status_code, 200)
        self.assertFalse(UpstreamRoute.objects.exists())

    def test_selectors_are_mutually_exclusive_for_every_method(self):
        for selector in ({}, {"source_host": HOST, "source_url": SOURCE},
                         {"source_host": HOST, "source_url": ""},
                         {"source_host": HOST, "source_url": None}):
            for method in ("post", "delete"):
                payload = {**selector, "expected_revision": 0}
                if method == "post":
                    payload["provider_order"] = ["parsehub_generic"]
                with self.subTest(selector=selector, method=method):
                    response = getattr(self.client, method)(self.endpoint, json.dumps(payload),
                                                            content_type="application/json")
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error_code"], "INVALID_SOURCE_SCOPE")
        for query in ({}, {"source_host": HOST, "source_url": SOURCE},
                      {"source_host": HOST, "source_url": ""}, {"source_host": [HOST, "douban.com"]},
                      {"source_url": [SOURCE, SOURCE]}):
            self.assertEqual(self.client.get(self.endpoint, query).status_code, 400)
        self.assertFalse(UpstreamRoute.objects.exists())

    def test_mature_and_blocked_hosts_cannot_be_overridden(self):
        for host in ("www.youtube.com", "www.xiaohongshu.com", "www.douyin.com", "www.bilibili.com",
                     "tiktok.com", "xhslink.com", "b23.tv", "xvideos.com"):
            with self.subTest(host=host):
                route = self.client.get(self.endpoint, {"source_host": host}).json()["route"]
                self.assertFalse(route["applicable"])
                self.assertEqual(source_scope("https://" + host + "/")["reason"], route["reason"])
                self.assertEqual(self.save(source_host=host).status_code, 422)
                self.assertEqual(self.reset(0, host).status_code, 422)
        self.assertFalse(UpstreamRoute.objects.exists())
        self.assertFalse(UpstreamRouteAudit.objects.exists())

    def test_common_unknown_and_malformed_modes_never_inherit_dedicated_options(self):
        from .services.provider_lab.videodl_policy import REVIEWED_COMMON_BACKEND_IDS

        dedicated = {"videodl_repo": {"mode": "specific_backend", "backend_id": "ZhihuVideoClient"}}
        self.assertEqual(self.save(provider_order=["videodl_repo"], provider_options=dedicated).status_code, 200)
        before = read_route(source_host=HOST)
        for option in ({"mode": "common"}, {"mode": "common", "backend_id": "ZhihuVideoClient"},
                       {"mode": "unknown"}, {"mode": "specific_backend", "backend_id": "UnknownClient"},
                       {"mode": "specific_backend", "backend_id": "CommonVideoClient"},
                       {"mode": "specific_backend", "backend_id": REVIEWED_COMMON_BACKEND_IDS[0]},
                       {"mode": "specific_backend", "backend_id": []}):
            with self.subTest(option=option):
                response = self.save(expected_revision=1, provider_order=["videodl_repo"],
                                     provider_options={"videodl_repo": option})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(read_route(source_host=HOST), before)
        self.assertEqual(self.save(expected_revision=1, provider_order=["unknown_provider"]).status_code, 400)
        self.assertEqual(UpstreamRouteAudit.objects.count(), 1)
        response = self.save(expected_revision=1, provider_order=["videodl_repo"])
        self.assertEqual(response.json()["route"]["provider_options"], {})

    def test_legacy_common_and_unknown_options_remain_visible_without_writes(self):
        stored = UpstreamRoute.objects.create(source_host=HOST, revision=7, active=True,
                                              provider_order=["videodl_repo"])
        for mode in ("common", "unknown"):
            options = {"videodl_repo": {"mode": mode, "backend_id": "ZhihuVideoClient"}}
            stored.provider_options = options
            stored.save(update_fields=["provider_options"])
            response = self.client.get(self.endpoint, {"source_host": HOST})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["route"]["provider_options"], options)
            self.assertEqual(self.save(expected_revision=7, provider_order=["videodl_repo"],
                                      provider_options=options).status_code, 400)
            stored.refresh_from_db()
            self.assertEqual(stored.revision, 7)
            self.assertEqual(stored.provider_options, options)
        self.assertFalse(UpstreamRouteAudit.objects.exists())

    def test_auth_permissions_and_csrf_apply_to_host_requests(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.endpoint, {"source_host": HOST}).status_code, 403)
        self.assertEqual(self.save().status_code, 403)
        self.assertEqual(self.reset(0).status_code, 403)
        for active, staff in ((True, False), (False, True)):
            user = get_user_model().objects.create_user(username=f"host-{active}-{staff}",
                                                        is_active=active, is_staff=staff)
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.endpoint, {"source_host": HOST}).status_code, 403)
            self.assertEqual(self.save().status_code, 403)
        reader = get_user_model().objects.create_user(username="host-reader", is_staff=True)
        self.client.force_login(reader)
        self.assertEqual(self.client.get(self.endpoint, {"source_host": HOST}).status_code, 200)
        self.assertEqual(self.save().status_code, 403)
        self.assertEqual(self.reset(0).status_code, 403)
        reader.user_permissions.add(Permission.objects.get(codename="change_upstreamroute"))
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.reset(1).status_code, 200)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        payload = json.dumps({"source_host": HOST, "expected_revision": 2})
        for method in ("post", "delete"):
            self.assertEqual(getattr(strict, method)(self.endpoint, payload,
                                                    content_type="application/json").status_code, 403)
        self.assertEqual(UpstreamRoute.objects.get().revision, 2)
        self.assertEqual(UpstreamRouteAudit.objects.count(), 2)
