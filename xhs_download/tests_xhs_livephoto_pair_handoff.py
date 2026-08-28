import time
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import signing
from django.core.cache import cache
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from .services.platforms.xhs_livephoto_builder import (
    build_xhs_livephoto_preview_context,
)
from .services.xhs_livephoto_pair_handoff import (
    XHS_LIVEPHOTO_PAIR_TOKEN_SALT,
    XHS_LIVEPHOTO_PAIR_MAX_USES,
    XhsLivePhotoPairHandoffError,
    _cache_key,
    _record_binding,
    get_xhs_livephoto_pair_client_identity,
    load_xhs_livephoto_pair_descriptor,
)
from .tests_xhs_livephoto_staff_vertical_slice import (
    enhanced_result,
    livephoto_item,
)


class XhsLivePhotoPairHandoffTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.client_identity = "client-fixture"
        self.result = enhanced_result([livephoto_item(0)])
        self.context = build_xhs_livephoto_preview_context(
            self.result,
            client_identity=self.client_identity,
        )
        self.token = self.context["items"][0]["pair_token"]

    def tearDown(self):
        cache.clear()

    def _payload_and_record(self):
        payload = signing.loads(
            self.token,
            salt=XHS_LIVEPHOTO_PAIR_TOKEN_SALT,
        )
        record = deepcopy(cache.get(_cache_key(payload["ticket_id"])))
        return payload, record

    def _resign_record(self, record):
        payload, _old_record = self._payload_and_record()
        cache.set(_cache_key(payload["ticket_id"]), record, timeout=120)
        return signing.dumps(
            {
                "ticket_id": payload["ticket_id"],
                "binding": _record_binding(record),
            },
            salt=XHS_LIVEPHOTO_PAIR_TOKEN_SALT,
            compress=False,
        )

    def test_valid_token_is_url_free_and_returns_exact_pair_descriptor(self):
        self.assertNotIn("http", self.token.lower())
        self.assertNotIn("xhscdn", self.token.lower())
        payload, _record = self._payload_and_record()
        self.assertEqual(set(payload), {"ticket_id", "binding"})

        descriptor = load_xhs_livephoto_pair_descriptor(
            self.token,
            client_identity=self.client_identity,
        )

        self.assertEqual(descriptor.still["media_type"], "image")
        self.assertEqual(descriptor.motion["media_type"], "video")
        self.assertEqual(descriptor.motion["format_class"], "mp4")
        self.assertRegex(descriptor.still["filename"], r"_01_still\.(?:jpg|webp)$")
        self.assertRegex(descriptor.motion["filename"], r"_01_motion\.mp4$")
        self.assertRegex(descriptor.archive_filename, r"_01_livephoto\.zip$")

    def test_preview_context_preserves_safe_pair_authority_error_code(self):
        def rejected_issuer(**_kwargs):
            raise XhsLivePhotoPairHandoffError(
                "rejected",
                error_code="XHS_LIVEPHOTO_PAIR_STILL_REJECTED",
                status_code=400,
            )

        context = build_xhs_livephoto_preview_context(
            self.result,
            client_identity=self.client_identity,
            issue_pair_token=rejected_issuer,
        )

        item = context["items"][0]
        self.assertEqual(item["delivery_status"], "unavailable")
        self.assertEqual(
            item["delivery_error_code"],
            "XHS_LIVEPHOTO_PAIR_STILL_REJECTED",
        )
        self.assertEqual(item["pair_token"], "")

    def test_preview_context_scrubs_unknown_pair_issuer_errors(self):
        def broken_issuer(**_kwargs):
            raise RuntimeError("private details")

        context = build_xhs_livephoto_preview_context(
            self.result,
            client_identity=self.client_identity,
            issue_pair_token=broken_issuer,
        )

        self.assertEqual(
            context["items"][0]["delivery_error_code"],
            "XHS_LIVEPHOTO_PAIR_AUTHORITY_UNAVAILABLE",
        )

    def test_missing_cached_record_is_expired_fail_closed(self):
        payload, _record = self._payload_and_record()
        cache.delete(_cache_key(payload["ticket_id"]))

        with self.assertRaises(XhsLivePhotoPairHandoffError) as raised:
            load_xhs_livephoto_pair_descriptor(
                self.token,
                client_identity=self.client_identity,
            )

        self.assertEqual(raised.exception.error_code, "XHS_LIVEPHOTO_PAIR_EXPIRED")

    def test_signed_token_max_age_and_cached_record_expiry_are_enforced(self):
        with patch("django.core.signing.time.time", return_value=time.time() + 121):
            with self.assertRaises(XhsLivePhotoPairHandoffError) as signed_expiry:
                load_xhs_livephoto_pair_descriptor(
                    self.token,
                    client_identity=self.client_identity,
                )
        self.assertEqual(signed_expiry.exception.error_code, "XHS_LIVEPHOTO_PAIR_EXPIRED")

        _payload, record = self._payload_and_record()
        record["expires_at"] = time.time() - 1
        token = self._resign_record(record)
        with self.assertRaises(XhsLivePhotoPairHandoffError) as record_expiry:
            load_xhs_livephoto_pair_descriptor(
                token,
                client_identity=self.client_identity,
            )
        self.assertEqual(record_expiry.exception.error_code, "XHS_LIVEPHOTO_PAIR_EXPIRED")

    def test_wrong_client_is_rejected(self):
        with self.assertRaises(XhsLivePhotoPairHandoffError) as raised:
            load_xhs_livephoto_pair_descriptor(
                self.token,
                client_identity="different-client",
            )

        self.assertEqual(
            raised.exception.error_code,
            "XHS_LIVEPHOTO_PAIR_CLIENT_MISMATCH",
        )

    def test_altered_group_and_asset_ids_are_rejected(self):
        for mutation in ("group", "still", "motion"):
            with self.subTest(mutation=mutation):
                self.setUp()
                _payload, record = self._payload_and_record()
                if mutation == "group":
                    record["group_id"] = record["group_id"].replace("i0", "i1")
                else:
                    record[mutation]["asset_id"] = record[mutation]["asset_id"].replace(
                        "i0", "i1"
                    )
                token = self._resign_record(record)
                with self.assertRaises(XhsLivePhotoPairHandoffError):
                    load_xhs_livephoto_pair_descriptor(
                        token,
                        client_identity=self.client_identity,
                    )

    def test_url_substitution_and_wrong_platform_are_rejected(self):
        for mutation in ("url", "platform"):
            with self.subTest(mutation=mutation):
                self.setUp()
                _payload, record = self._payload_and_record()
                if mutation == "url":
                    record["motion"]["url"] = "https://lookalike.example/video.mp4"
                else:
                    record["still"]["source_platform"] = "douyin"
                token = self._resign_record(record)
                with self.assertRaises(XhsLivePhotoPairHandoffError):
                    load_xhs_livephoto_pair_descriptor(
                        token,
                        client_identity=self.client_identity,
                    )

    def test_original_token_rejects_cached_record_tampering(self):
        payload, record = self._payload_and_record()
        record["base_filename"] = "tampered"
        cache.set(_cache_key(payload["ticket_id"]), record, timeout=120)

        with self.assertRaises(XhsLivePhotoPairHandoffError) as raised:
            load_xhs_livephoto_pair_descriptor(
                self.token,
                client_identity=self.client_identity,
            )

        self.assertEqual(
            raised.exception.error_code,
            "XHS_LIVEPHOTO_PAIR_BINDING_REJECTED",
        )

    def test_use_limit_and_native_still_format_scope_are_enforced(self):
        for _index in range(XHS_LIVEPHOTO_PAIR_MAX_USES):
            load_xhs_livephoto_pair_descriptor(
                self.token,
                client_identity=self.client_identity,
            )
        with self.assertRaises(XhsLivePhotoPairHandoffError) as replay:
            load_xhs_livephoto_pair_descriptor(
                self.token,
                client_identity=self.client_identity,
            )
        self.assertEqual(replay.exception.error_code, "XHS_LIVEPHOTO_PAIR_USE_LIMIT")

        self.setUp()
        _payload, record = self._payload_and_record()
        record["still"]["extension"] = "png"
        token = self._resign_record(record)
        with self.assertRaises(XhsLivePhotoPairHandoffError) as format_error:
            load_xhs_livephoto_pair_descriptor(
                token,
                client_identity=self.client_identity,
            )
        self.assertEqual(
            format_error.exception.error_code,
            "XHS_LIVEPHOTO_PAIR_STILL_REJECTED",
        )

    def test_pair_identity_combines_network_and_browser_csrf_secret(self):
        factory = RequestFactory()

        def identity(secret, user_id=1):
            request = factory.get("/", REMOTE_ADDR="192.0.2.1")
            request.user = User(
                pk=user_id,
                username=f"staff-{user_id}",
                is_staff=True,
                is_active=True,
            )
            request.META["CSRF_COOKIE"] = secret
            return get_xhs_livephoto_pair_client_identity(request)

        self.assertEqual(identity("a" * 32), identity("a" * 32))
        self.assertNotEqual(identity("a" * 32), identity("b" * 32))
        self.assertNotEqual(identity("a" * 32, 1), identity("a" * 32, 2))


class XhsLivePhotoPairDescriptorViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.staff = User.objects.create_user(
            username="pair-staff",
            password="test-password",
            is_staff=True,
            is_active=True,
        )
        self.client.force_login(self.staff)
        request = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1")
        request.user = self.staff
        identity = get_xhs_livephoto_pair_client_identity(request)
        self.client.cookies["csrftoken"] = request.META["CSRF_COOKIE"]
        result = enhanced_result([livephoto_item(0)])
        context = build_xhs_livephoto_preview_context(
            result,
            client_identity=identity,
        )
        self.token = context["items"][0]["pair_token"]
        self.url = reverse("media_xhs_livephoto_pair_descriptor")

    def tearDown(self):
        cache.clear()

    def test_post_returns_control_descriptor_without_server_media_fetch(self):
        with patch("requests.get") as media_get:
            response = self.client.post(
                self.url,
                data={"pair_token": self.token},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["still"]["media_type"], "image")
        self.assertEqual(payload["motion"]["media_type"], "video")
        self.assertNotIn("provider", payload["still"])
        self.assertNotIn("provider", payload["motion"])
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        media_get.assert_not_called()

    def test_endpoint_is_post_only_and_csrf_protected(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        response = csrf_client.post(
            self.url,
            data={"pair_token": self.token},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_nonstaff_and_inactive_staff_cannot_use_stolen_token(self):
        anonymous = Client()
        anonymous.cookies["csrftoken"] = self.client.cookies["csrftoken"].value

        nonstaff = User.objects.create_user(
            username="pair-nonstaff",
            password="test-password",
            is_active=True,
        )
        nonstaff_client = Client()
        nonstaff_client.force_login(nonstaff)
        nonstaff_client.cookies["csrftoken"] = self.client.cookies["csrftoken"].value

        inactive_staff = User.objects.create_user(
            username="pair-inactive-staff",
            password="test-password",
            is_staff=True,
            is_active=False,
        )
        inactive_client = Client()
        inactive_client.force_login(inactive_staff)
        inactive_client.cookies["csrftoken"] = self.client.cookies["csrftoken"].value

        for client in (anonymous, nonstaff_client, inactive_client):
            with self.subTest(client=client):
                response = client.post(
                    self.url,
                    data={"pair_token": self.token},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.json()["error_code"],
                    "XHS_LIVEPHOTO_PAIR_STAFF_REQUIRED",
                )

    def test_token_is_bound_to_the_issuing_staff_user(self):
        other_staff = User.objects.create_user(
            username="pair-other-staff",
            password="test-password",
            is_staff=True,
            is_active=True,
        )
        other_client = Client()
        other_client.force_login(other_staff)
        other_client.cookies["csrftoken"] = self.client.cookies["csrftoken"].value

        response = other_client.post(
            self.url,
            data={"pair_token": self.token},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["error_code"],
            "XHS_LIVEPHOTO_PAIR_CLIENT_MISMATCH",
        )

    def test_endpoint_rejects_extra_fields(self):
        response = self.client.post(
            self.url,
            data={"pair_token": self.token, "motion_url": "hidden"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("hidden", response.content.decode("utf-8"))
    get_xhs_livephoto_pair_client_identity,
