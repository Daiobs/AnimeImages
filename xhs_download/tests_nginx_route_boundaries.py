"""Explicit media policy classification; never generate public Nginx routes."""

from django.conf import settings
from django.test import Client, SimpleTestCase
from django.urls import URLResolver, get_resolver, resolve

from ops.nginx.tools import check_nginx_drift as drift


# A new Django media route must receive a deliberate policy review here.
BUSINESS_MEDIA_ROUTES = {
    "media/upstream-candidate/": ("=", "/media/upstream-candidate/"),
    "media/xhs-livephoto-motion-descriptor/": ("=", "/media/xhs-livephoto-motion-descriptor/"),
    "media/xhs-livephoto-pair-descriptor/": ("=", "/media/xhs-livephoto-pair-descriptor/"),
    "media/ytdlp-selected-format/": ("=", "/media/ytdlp-selected-format/"),
    "media/service-selected-format/": ("=", "/media/service-selected-format/"),
    "media/browser-download-telemetry/": ("=", "/media/browser-download-telemetry/"),
    "media/douyin-continuation/": ("^~", "/media/douyin-continuation/"),
    "media/download-prepare/": ("^~", "/media/download-prepare/"),
    "media/download-status/<str:token>/": ("^~", "/media/download-status/"),
    "media/download-stream/<str:token>/": ("^~", "/media/download-stream/"),
    "media/proxy-image/": ("/media/proxy-image/",),
    "media/download-images/": ("/media/download-images/",),
    "media/download-tasks/": ("/media/download-tasks/",),
    "media/download-tasks/<str:task_id>/": ("/media/download-tasks/",),
    "media/download-tasks/<str:task_id>/file/": ("/media/download-tasks/",),
}
DENIED_MEDIA_ROUTES = {
    "^media/(?P<path>.*)$": "DEBUG-only uploaded files; production /media/ must deny them",
}
NEW_MEDIA_PATHS = (
    "/media/upstream-candidate/",
    "/media/xhs-livephoto-motion-descriptor/",
    "/media/xhs-livephoto-pair-descriptor/",
    "/media/ytdlp-selected-format/",
    "/media/service-selected-format/",
)


def media_url_patterns(patterns, prefix=""):
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from media_url_patterns(pattern.url_patterns, route)
        elif route.lstrip("^").startswith("media/"):
            yield route


class NginxMediaPolicyTests(SimpleTestCase):
    def test_every_django_media_route_has_an_explicit_policy(self):
        actual = set(media_url_patterns(get_resolver().url_patterns))
        classified = set(BUSINESS_MEDIA_ROUTES) | set(DENIED_MEDIA_ROUTES)
        self.assertEqual(actual - classified, set(), "classify new media routes; do not auto-expose them")
        self.assertEqual(set(BUSINESS_MEDIA_ROUTES) - actual, set())
        self.assertEqual(set(BUSINESS_MEDIA_ROUTES) & set(DENIED_MEDIA_ROUTES), set())

    def test_business_routes_have_the_reviewed_nginx_proxy_location(self):
        site = drift.render_site_template(drift.SITE_TEMPLATE_PATH.read_text(), "192.0.2.10")
        directives = drift.parse_nginx(site) + drift.parse_nginx(drift.MEDIA_SNIPPET_PATH.read_text())
        locations = {item.args: item for item in drift.walk(directives) if item.name == "location"}
        for route, args in BUSINESS_MEDIA_ROUTES.items():
            with self.subTest(route=route):
                self.assertIn(args, set(locations))
                proxies = drift.direct_children(locations[args], "proxy_pass")
                self.assertEqual([proxy.args for proxy in proxies], [(drift.LOCAL_APP_UPSTREAM,)])
        broad_media = locations[("/media/",)]
        self.assertEqual([item.args for item in drift.direct_children(broad_media, "return")], [("404",)])
        self.assertFalse(drift.direct_children(broad_media, "proxy_pass"))


class NginxMediaApplicationGuardTests(SimpleTestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def post_with_csrf(self, path, payload):
        token = "a" * 32
        self.client.cookies[settings.CSRF_COOKIE_NAME] = token
        return self.client.post(path, payload, content_type="application/json", HTTP_X_CSRFTOKEN=token)

    def test_all_five_real_views_reject_wrong_methods(self):
        for path in NEW_MEDIA_PATHS:
            for method in ("get", "head"):
                with self.subTest(path=path, method=method):
                    response = getattr(self.client, method)(path)
                    self.assertEqual(response.status_code, 405)
                    self.assertEqual(response["Allow"], "POST")
                    self.assertEqual(response.resolver_match.func, resolve(path).func)

    def test_all_five_real_views_require_csrf(self):
        for path in NEW_MEDIA_PATHS:
            with self.subTest(path=path):
                response = self.client.post(path, {}, content_type="application/json")
                self.assertEqual(response.status_code, 403)
                self.assertIn("text/html", response["Content-Type"])

    def test_livephoto_requires_staff_even_with_valid_csrf(self):
        for path, token, code in (
            (NEW_MEDIA_PATHS[1], "motion_token", "XHS_LIVEPHOTO_PLAYBACK_STAFF_REQUIRED"),
            (NEW_MEDIA_PATHS[2], "pair_token", "XHS_LIVEPHOTO_PAIR_STAFF_REQUIRED"),
        ):
            with self.subTest(path=path):
                response = self.post_with_csrf(path, {token: "invalid-token"})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error_code"], code)
                self.assertIn("no-store", response["Cache-Control"])

    def test_candidate_routes_reject_missing_or_invalid_resource_credentials(self):
        for path in (NEW_MEDIA_PATHS[0], NEW_MEDIA_PATHS[3], NEW_MEDIA_PATHS[4]):
            for credentials in ({}, {"candidate_id": "invalid", "resource_token": "invalid"}):
                payload = dict(credentials)
                if path != NEW_MEDIA_PATHS[0]:
                    payload["action"] = "download"
                with self.subTest(path=path, credentials=bool(credentials)):
                    response = self.post_with_csrf(path, payload)
                    expected = 400 if not credentials and path != NEW_MEDIA_PATHS[0] else 403
                    self.assertEqual(response.status_code, expected)
                    self.assertEqual(response.json()["status"], "error")
                    self.assertIn("no-store", response["Cache-Control"])
