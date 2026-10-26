"""Contract sections 5/9 and M4: resource grants, not delivery permission."""
import base64
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import TestCase
from unittest.mock import patch

from xhs_download.services.pipeline.delivery import (
    Delivery, Method, decide_archive, decide_delivery,
)
from xhs_download.services.pipeline.grant import (
    Grant, GrantError, issue_grant, verify_grant, verify_hop,
)
from xhs_download.services.pipeline.model import (
    Fetchable, Item, Kind, Tracks, Variant, Work, validate_work,
)


KEY = b"contract-only-key-not-for-production"
NOW = datetime(2026, 10, 26, 12, 0, 0, 123456, tzinfo=timezone.utc)
URL = "https://media.cdn.example/clip.mp4?download=1"
IP_HOSTS = (
    "127.0.0.1", "8.8.8.8", "[::1]", "[2001:4860:4860::8888]",
    "127.1", "2130706433", "0x7f000001", "0177.0.0.1", "134744072",
)


def media(**changes):
    return replace(Variant(
        variant_id="v-main", url=URL, container="mp4", tracks=Tracks.AV,
        browser_fetchable=Fetchable.TRUE,
        host_policy=("cdn.example", "mirror.example"),
        required_headers=("Cookie", "X-Media-Token", "Referer"),
    ), **changes)


def work_with(selected=None, *, kind=Kind.VIDEO, others=(), **changes):
    selected = selected if selected is not None else media()
    return replace(Work(
        source_url="https://source.example/posts/w-17", work_id="w-17",
        platform="platform-a", title="A private title", description="Private description",
        provider="provider-a", items=(Item(
            item_id="i-main", kind=kind, index=1, variants=(selected, *others),
        ),),
    ), **changes)


def reference_payload():
    return {
        "version": 1, "url": URL,
        "host_policy": ["cdn.example", "mirror.example"],
        "platform": "platform-a", "work_id": "w-17", "variant_id": "v-main",
        "required_headers": ["Cookie", "X-Media-Token", "Referer"],
        "issued_at": "2026-10-26T12:00:00.123456+00:00",
        "expires_at": "2026-10-26T12:05:00.123456+00:00",
    }


def canonical_bytes(payload):
    return json.dumps(payload, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def sign_bytes(raw, *, domain=b"pipeline-download-grant:v1\0", key=KEY):
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    digest = hmac.new(key, domain + encoded, hashlib.sha256).hexdigest()
    return encoded.decode("ascii") + "." + digest


def signed_payload(**changes):
    return sign_bytes(canonical_bytes(dict(reference_payload(), **changes)))


def expected_grant(**changes):
    return replace(Grant(
        url=URL, host_policy=("cdn.example", "mirror.example"),
        platform="platform-a", work_id="w-17", variant_id="v-main",
        required_headers=("Cookie", "X-Media-Token", "Referer"),
        issued_at=NOW, expires_at=NOW + timedelta(seconds=300),
    ), **changes)


class GrantContract(TestCase):
    def assert_rejected(self, function, *args, **kwargs):
        with self.assertRaises(GrantError) as caught:
            function(*args, **kwargs)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertRegex(str(caught.exception), r"\A[A-Z][A-Z0-9_]*\Z")
        return caught.exception

    def test_canonical_wire_format_and_independently_signed_claims(self):
        raw = canonical_bytes(reference_payload())
        token = issue_grant(work_with(), "v-main", key=KEY, now=NOW)
        self.assertEqual(token, sign_bytes(raw))
        encoded, signature = token.split(".")
        self.assertRegex(encoded, r"\A[A-Za-z0-9_-]+\Z")
        self.assertRegex(signature, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)), raw)
        self.assertEqual(verify_grant(signed_payload(), key=KEY, now=NOW), expected_grant())
        self.assertEqual(verify_hop(token, URL, key=KEY, now=NOW), expected_grant())

    def test_purpose_domain_and_encoded_not_decoded_mac_input(self):
        raw = canonical_bytes(reference_payload())
        for domain in (b"", b"other-purpose:v1\0", b"pipeline-download-grant:v1"):
            with self.subTest(domain=domain):
                token = sign_bytes(raw, domain=domain)
                self.assert_rejected(verify_grant, token, key=KEY, now=NOW)
                self.assert_rejected(verify_hop, token, URL, key=KEY, now=NOW)
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        wrong_digest = hmac.new(KEY, b"pipeline-download-grant:v1\0" + raw,
                                hashlib.sha256).hexdigest()
        self.assert_rejected(verify_grant, encoded + "." + wrong_digest, key=KEY, now=NOW)

    def test_each_claim_is_authenticated_by_verify_and_every_hop(self):
        original = signed_payload()
        signature = original.split(".")[1]
        changes = {
            "version": 2, "url": URL + "&changed=1", "host_policy": [],
            "platform": "platform-b", "work_id": "w-18", "variant_id": "v-other",
            "required_headers": [], "issued_at": "2026-10-26T11:59:59.123456+00:00",
            "expires_at": "2026-10-26T12:04:00.123456+00:00",
        }
        self.assertEqual(set(changes), set(reference_payload()))
        for field, value in changes.items():
            with self.subTest(field=field):
                encoded = signed_payload(**{field: value}).split(".")[0]
                tampered = encoded + "." + signature
                self.assert_rejected(verify_grant, tampered, key=KEY, now=NOW)
                for redirect, target in ((False, URL), (True, "https://edge.cdn.example/end")):
                    self.assert_rejected(verify_hop, tampered, target, key=KEY, now=NOW,
                                         is_redirect=redirect)

    def test_wrong_and_invalid_keys_are_rejected_at_all_entries(self):
        token = signed_payload()
        for key in (b"wrong-secret", b"", "not-bytes", bytearray(KEY), None):
            with self.subTest(key=type(key).__name__, value=repr(key)):
                self.assert_rejected(verify_grant, token, key=key, now=NOW)
                self.assert_rejected(verify_hop, token, URL, key=key, now=NOW)
                if key != b"wrong-secret":
                    self.assert_rejected(issue_grant, work_with(), "v-main", key=key, now=NOW)

    def test_malformed_token_envelopes_have_only_safe_errors(self):
        encoded, signature = signed_payload().split(".")
        tokens = (None, 7, b"bytes", "", "missing-dot", "." + signature,
                  encoded + ".", signed_payload() + ".extra", "!" + encoded + "." + signature,
                  encoded + "=." + signature, encoded + "." + signature.upper(),
                  encoded + "." + signature[:-1], encoded + "." + signature + "0",
                  encoded + "." + "z" * 64, signed_payload() + "\n")
        for token in tokens:
            with self.subTest(token=repr(token)[:90]):
                self.assert_rejected(verify_grant, token, key=KEY, now=NOW)

    def test_valid_mac_does_not_authorize_unknown_version_or_schema(self):
        payloads = [[], None, "not-an-object"]
        payloads.extend(dict(reference_payload(), version=value) for value in (2, True, "1", 1.0))
        payloads.append(dict(reference_payload(), unexpected="must-not-be-ignored"))
        for field in reference_payload():
            payloads.append({name: value for name, value in reference_payload().items() if name != field})
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assert_rejected(verify_grant, sign_bytes(canonical_bytes(payload)), key=KEY, now=NOW)

    def test_valid_mac_does_not_authorize_noncanonical_or_invalid_json(self):
        raw = canonical_bytes(reference_payload())
        for altered in (b"{", b"\xff", b" " + raw, raw + b"\n",
                        b'{"version":1,' + raw[1:]):
            with self.subTest(raw=altered[:60]):
                self.assert_rejected(verify_grant, sign_bytes(altered), key=KEY, now=NOW)

    def test_grant_is_frozen_with_tuple_claims_and_aware_utc_times(self):
        grant = verify_grant(signed_payload(), key=KEY, now=NOW)
        self.assertEqual(grant, expected_grant())
        self.assertIs(type(grant.host_policy), tuple)
        self.assertIs(type(grant.required_headers), tuple)
        self.assertIs(grant.issued_at.tzinfo, timezone.utc)
        self.assertIs(grant.expires_at.tzinfo, timezone.utc)
        for name in reference_payload().keys() - {"version"}:
            with self.subTest(field=name), self.assertRaises(FrozenInstanceError):
                setattr(grant, name, None)

    def test_expiry_is_exclusive_and_each_hop_rechecks_current_time(self):
        token = signed_payload()
        expiry = NOW + timedelta(seconds=300)
        targets = ((False, URL), (True, "https://edge.cdn.example/hop-1"),
                   (True, "https://mirror.example/hop-2"))
        for redirect, target in targets:
            with self.subTest(target=target):
                self.assertEqual(verify_hop(token, target, key=KEY, now=expiry - timedelta(microseconds=1),
                                            is_redirect=redirect), expected_grant())
                for instant in (NOW - timedelta(microseconds=1), expiry, expiry + timedelta(seconds=1)):
                    self.assert_rejected(verify_hop, token, target, key=KEY, now=instant,
                                         is_redirect=redirect)
        self.assertEqual(verify_grant(token, key=KEY, now=NOW), expected_grant())
        self.assert_rejected(verify_grant, token, key=KEY, now=expiry)

    def test_upstream_expiry_caps_ttl_without_rounding_away_microseconds(self):
        short = NOW + timedelta(seconds=17, microseconds=23)
        for upstream, ttl, expected in ((None, 300, NOW + timedelta(seconds=300)),
                                        (short, 300, short), (short, 1, NOW + timedelta(seconds=1)),
                                        (NOW + timedelta(days=1), 300, NOW + timedelta(seconds=300))):
            with self.subTest(upstream=upstream, ttl=ttl):
                token = issue_grant(work_with(media(expires_at=upstream)), "v-main",
                                    key=KEY, now=NOW, ttl_seconds=ttl)
                self.assertEqual(verify_grant(token, key=KEY, now=NOW), expected_grant(expires_at=expected))
                self.assert_rejected(verify_grant, token, key=KEY, now=expected)
        for upstream in (NOW, NOW - timedelta(microseconds=1)):
            self.assert_rejected(issue_grant, work_with(media(expires_at=upstream)), "v-main", key=KEY, now=NOW)

    def test_ttl_limit_applies_to_issuance_and_signed_tokens(self):
        for ttl in (0, -1, 301, 3600, True, 1.0, "300", None):
            with self.subTest(ttl=ttl):
                self.assert_rejected(issue_grant, work_with(), "v-main", key=KEY, now=NOW, ttl_seconds=ttl)
        too_long = signed_payload(expires_at="2026-10-26T12:05:00.123457+00:00")
        self.assert_rejected(verify_grant, too_long, key=KEY, now=NOW)
        for expiry in ("2026-10-26T12:00:00.123456+00:00", "2026-10-26T11:59:59.123456+00:00"):
            self.assert_rejected(verify_grant, signed_payload(expires_at=expiry), key=KEY, now=NOW)

    def test_timezone_conversion_and_noncanonical_or_naive_times(self):
        local = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(issue_grant(work_with(), "v-main", key=KEY, now=local), signed_payload())
        self.assertEqual(verify_grant(signed_payload(), key=KEY, now=local), expected_grant())
        for invalid in (NOW.replace(tzinfo=None), "2026-10-26", None):
            self.assert_rejected(issue_grant, work_with(), "v-main", key=KEY, now=invalid)
            self.assert_rejected(verify_grant, signed_payload(), key=KEY, now=invalid)
            self.assert_rejected(verify_hop, signed_payload(), URL, key=KEY, now=invalid)
        self.assert_rejected(issue_grant, work_with(media(expires_at=NOW.replace(tzinfo=None))),
                             "v-main", key=KEY, now=NOW)
        for field in ("issued_at", "expires_at"):
            for value in (None, 123, "2026-10-26T12:00:00", "2026-10-26T12:00:00+00:00",
                          "2026-10-26T12:00:00.123456Z", "2026-10-26T17:30:00.123456+05:30"):
                with self.subTest(field=field, value=value):
                    self.assert_rejected(verify_grant, signed_payload(**{field: value}), key=KEY, now=NOW)

    def test_only_selected_variant_from_valid_work_can_be_issued(self):
        selected = media(variant_id="v-low", url="https://cdn.example/low.mp4", height=360)
        better = media(variant_id="v-high", url="https://cdn.example/high.mp4", height=2160)
        work = work_with(selected, others=(better,))
        self.assertEqual(validate_work(work), ())
        self.assertEqual(work.items[0].default_variant, better)
        token = issue_grant(work, "v-low", key=KEY, now=NOW)
        self.assertEqual(verify_grant(token, key=KEY, now=NOW),
                         expected_grant(url=selected.url, variant_id="v-low"))
        self.assert_rejected(issue_grant, work, "absent", key=KEY, now=NOW)
        for invalid in (reference_payload(), media(), None, replace(work, items=()),
                        work_with(selected, others=(selected,))):
            self.assert_rejected(issue_grant, invalid, "v-low", key=KEY, now=NOW)

    def test_rule_zero_blocks_each_reason_inside_otherwise_valid_work(self):
        cases = ((dict(drm=True), "DRM_PROTECTED"),
                 (dict(segmented=True), "SEGMENTED_STREAM"),
                 (dict(request_method="POST"), "UNSUPPORTED_REQUEST"),
                 (dict(tracks=Tracks.VIDEO_ONLY, merge_with=""), "NEEDS_AUDIO_MERGE"))
        backup = media(variant_id="backup", required_headers=())
        for changes, reason in cases:
            with self.subTest(reason=reason):
                blocked = media(**changes)
                work = work_with(blocked, others=(backup,))
                self.assertEqual(validate_work(work), ())
                self.assertEqual(work.items[0].default_variant, backup)
                self.assertEqual(decide_delivery(Kind.VIDEO, blocked), Delivery(0, Method.NONE, reason=reason))
                self.assert_rejected(issue_grant, work, "v-main", key=KEY, now=NOW)
                token = issue_grant(work, "backup", key=KEY, now=NOW)
                self.assertEqual(verify_grant(token, key=KEY, now=NOW),
                                 expected_grant(variant_id="backup", required_headers=()))

    def test_valid_split_partner_gets_grants_but_still_requires_server_merge(self):
        video = media(tracks=Tracks.VIDEO_ONLY, merge_with="audio", required_headers=())
        audio = media(variant_id="audio", url="https://cdn.example/audio.m4a",
                      container="m4a", tracks=Tracks.AUDIO_ONLY, required_headers=())
        work = work_with(video, others=(audio,))
        self.assertEqual(validate_work(work), ())
        for variant in (video, audio):
            token = issue_grant(work, variant.variant_id, key=KEY, now=NOW)
            self.assertEqual(verify_grant(token, key=KEY, now=NOW),
                             expected_grant(url=variant.url, variant_id=variant.variant_id, required_headers=()))
        self.assertEqual(decide_delivery(Kind.VIDEO, video), Delivery(1, Method.SERVER_MERGE))
        self.assertEqual(decide_archive(((Kind.VIDEO, video), (Kind.AUDIO, audio))),
                         Delivery(1, Method.NONE, reason="ARCHIVE_NEEDS_MERGE"))
        invalid = work_with(replace(video, merge_with="missing"), others=(audio,))
        self.assertIn("merge_partner_not_audio", validate_work(invalid))
        self.assert_rejected(issue_grant, invalid, "v-main", key=KEY, now=NOW)

    def test_grants_do_not_override_other_delivery_or_archive_decisions(self):
        cases = (
            (Kind.VIDEO, media(), Delivery(2, Method.SERVER_ATTACHMENT)),
            (Kind.VIDEO, media(required_headers=()), Delivery(4, Method.BROWSER_STREAMING, Method.BROWSER_BLOB)),
            (Kind.IMAGE, media(required_headers=()), Delivery(5, Method.BROWSER_DIRECT)),
            (Kind.VIDEO, media(required_headers=(), browser_fetchable=Fetchable.UNKNOWN),
             Delivery(6, Method.BROWSER_STREAMING, Method.SERVER_ATTACHMENT)),
            (Kind.VIDEO, media(required_headers=(), browser_fetchable=Fetchable.FALSE),
             Delivery(7, Method.NONE, reason="BROWSER_CANNOT_READ")),
        )
        for kind, variant, delivery in cases:
            with self.subTest(kind=kind, delivery=delivery):
                work = work_with(variant, kind=kind)
                token = issue_grant(work, "v-main", key=KEY, now=NOW)
                self.assertEqual(verify_grant(token, key=KEY, now=NOW),
                                 expected_grant(required_headers=variant.required_headers))
                self.assertEqual(decide_delivery(kind, variant), delivery)
        images = tuple(media(variant_id=f"image-{n}", required_headers=()) for n in (1, 2))
        work = work_with(items=tuple(Item(f"i-{n}", Kind.IMAGE, n, (variant,))
                                    for n, variant in enumerate(images, 1)))
        for variant in images:
            token = issue_grant(work, variant.variant_id, key=KEY, now=NOW)
            self.assertEqual(verify_grant(token, key=KEY, now=NOW),
                             expected_grant(variant_id=variant.variant_id, required_headers=()))
        self.assertEqual(decide_archive(tuple((Kind.IMAGE, variant) for variant in images)),
                         Delivery(3, Method.BROWSER_ARCHIVE, archive=True))

    def test_presentation_source_and_provider_metadata_never_enter_token(self):
        original = work_with()
        changed = replace(original, title="Other private title", description="Secret description",
                          source_url="https://private.example/source?secret=source-value",
                          topics=("private-topic",), author="private-author",
                          cover_url="https://private.example/cover", provider="other-provider",
                          field_sources={"title": "private-source"})
        token = issue_grant(changed, "v-main", key=KEY, now=NOW)
        self.assertEqual(token, issue_grant(original, "v-main", key=KEY, now=NOW))
        self.assertEqual(token, signed_payload())
        self.assertEqual(verify_grant(token, key=KEY, now=NOW), expected_grant())

    def test_headers_accept_names_only_not_mappings_values_or_crlf(self):
        names = ("Cookie", "Authorization", "Referer", "X-Token_1", "!#$%&'*+.^_`|~")
        token = issue_grant(work_with(media(required_headers=names)), "v-main", key=KEY, now=NOW)
        self.assertEqual(verify_grant(token, key=KEY, now=NOW), expected_grant(required_headers=names))
        for invalid in ({"Cookie": "session=secret"}, "Cookie", None, ("Cookie: session=secret",),
                        ("X-Auth\r\nCookie",), ("X-Auth\n",), ("Bad Name",), ("",), (7,),
                        (("Cookie", "session=secret"),)):
            with self.subTest(headers=invalid):
                self.assert_rejected(issue_grant, work_with(media(required_headers=invalid)),
                                     "v-main", key=KEY, now=NOW)
                self.assert_rejected(verify_grant, signed_payload(required_headers=invalid), key=KEY, now=NOW)

    def test_invalid_policy_is_rejected_not_silently_made_unrestricted(self):
        invalid_policies = (None, "cdn.example", {"cdn.example": True}, [""], ["*.cdn.example"],
                            [".cdn.example"], ["https://cdn.example"], ["cdn.example/path"],
                            ["cdn.example:443"], ["cdn.example\r\n"], ["bad name.example"],
                            ["cdn.example", ""], ["127.0.0.1"], ["127.1"], [7])
        for policy in invalid_policies:
            with self.subTest(policy=policy):
                self.assert_rejected(issue_grant, work_with(media(host_policy=policy)), "v-main", key=KEY, now=NOW)
                self.assert_rejected(verify_grant, signed_payload(host_policy=policy), key=KEY, now=NOW)

    def test_first_hop_is_exact_url_not_just_authorized_host(self):
        token = signed_payload()
        self.assertEqual(verify_hop(token, URL, key=KEY, now=NOW), expected_grant())
        for target in ("https://media.cdn.example/other.mp4?download=1",
                       "https://media.cdn.example/clip.mp4?download=2",
                       "https://media.cdn.example:443/clip.mp4?download=1",
                       "https://media.cdn.example:8443/clip.mp4?download=1",
                       "https://edge.cdn.example/clip.mp4?download=1",
                       "http://media.cdn.example/clip.mp4?download=1", URL + "#fragment"):
            with self.subTest(target=target):
                self.assert_rejected(verify_hop, token, target, key=KEY, now=NOW)

    def test_nonempty_policy_applies_to_initial_url_and_every_redirect(self):
        outside = "https://outside.example/clip.mp4"
        self.assert_rejected(issue_grant, work_with(media(url=outside)), "v-main", key=KEY, now=NOW)
        outside_token = signed_payload(url=outside)
        self.assert_rejected(verify_grant, outside_token, key=KEY, now=NOW)
        self.assert_rejected(verify_hop, outside_token, outside, key=KEY, now=NOW)
        token = signed_payload()
        for host in ("cdn.example", "a.b.cdn.example", "mirror.example", "42.cdn.example"):
            self.assertEqual(verify_hop(token, f"https://{host}/next", key=KEY, now=NOW,
                                        is_redirect=True), expected_grant())
        for host in ("evil-cdn.example", "evilcdn.example", "cdn.example.evil.test", "outside.example"):
            with self.subTest(host=host):
                self.assert_rejected(verify_hop, token, f"https://{host}/end", key=KEY, now=NOW,
                                     is_redirect=True)

    def test_empty_policy_allows_cross_host_https_redirect_but_not_http(self):
        token = issue_grant(work_with(media(host_policy=())), "v-main", key=KEY, now=NOW)
        expected = expected_grant(host_policy=())
        self.assertEqual(verify_hop(token, URL, key=KEY, now=NOW), expected)
        for target in ("https://unrelated.example/one", "https://second.example/two"):
            self.assertEqual(verify_hop(token, target, key=KEY, now=NOW, is_redirect=True), expected)
        for target in ("http://unrelated.example/one", "http://media.cdn.example/same-host"):
            self.assert_rejected(verify_hop, token, target, key=KEY, now=NOW, is_redirect=True)

    def test_original_http_attachments_and_nonempty_policy_http_redirects_remain_valid(self):
        original = "http://media.cdn.example:8080/clip.mp4?download=1"
        for policy in ((), ("cdn.example",)):
            with self.subTest(policy=policy):
                variant = media(url=original, host_policy=policy)
                token = issue_grant(work_with(variant), "v-main", key=KEY, now=NOW)
                expected = expected_grant(url=original, host_policy=policy)
                self.assertEqual(verify_grant(token, key=KEY, now=NOW), expected)
                self.assertEqual(verify_hop(token, original, key=KEY, now=NOW), expected)
                self.assertEqual(decide_delivery(Kind.VIDEO, variant), Delivery(2, Method.SERVER_ATTACHMENT))
                target = "http://edge.cdn.example:8080/next"
                if policy:
                    self.assertEqual(verify_hop(token, target, key=KEY, now=NOW, is_redirect=True), expected)
                else:
                    self.assert_rejected(verify_hop, token, target, key=KEY, now=NOW, is_redirect=True)

    def test_unsupported_schemes_credentials_and_malformed_urls_are_rejected(self):
        for url in ("ftp://cdn.example/a", "file:///tmp/a", "//cdn.example/a", "/relative",
                    "https://user:secret@cdn.example/a", "https://cdn.example:bad/a",
                    "https://cdn.example:65536/a", "https://cdn.example/a\r\nX-Secret:value",
                    "https://cdn.example/a b", "https://[broken/a"):
            with self.subTest(url=url):
                self.assert_rejected(issue_grant, work_with(media(url=url)), "v-main", key=KEY, now=NOW)
                self.assert_rejected(verify_grant, signed_payload(url=url), key=KEY, now=NOW)
                self.assert_rejected(verify_hop, signed_payload(host_policy=[]), url,
                                     key=KEY, now=NOW, is_redirect=True)

    def test_token_length_limit_applies_to_issuer_and_verifier(self):
        payload = reference_payload()
        # 49103 bytes encode to 65471 unpadded base64 chars, plus dot and HEX64.
        payload["work_id"] += "w" * (49103 - len(canonical_bytes(payload)))
        token = sign_bytes(canonical_bytes(payload))
        self.assertEqual(len(token), 65536)
        work = work_with(work_id=payload["work_id"])
        self.assertEqual(issue_grant(work, "v-main", key=KEY, now=NOW), token)
        self.assertEqual(verify_grant(token, key=KEY, now=NOW), expected_grant(work_id=work.work_id))
        payload["work_id"] += "w"
        too_long = sign_bytes(canonical_bytes(payload))
        self.assertGreater(len(too_long), 65536)
        self.assert_rejected(verify_grant, too_long, key=KEY, now=NOW)
        self.assert_rejected(issue_grant, replace(work, work_id=payload["work_id"]), "v-main", key=KEY, now=NOW)

    def test_no_dns_or_socket_io_during_issue_verify_or_hops(self):
        with ExitStack() as stack:
            boundaries = [stack.enter_context(patch("socket." + name, side_effect=AssertionError("network I/O")))
                          for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
                                       "create_connection", "socket.connect", "socket.connect_ex")]
            token = issue_grant(work_with(), "v-main", key=KEY, now=NOW)
            self.assertEqual(verify_grant(token, key=KEY, now=NOW), expected_grant())
            self.assertEqual(verify_hop(token, URL, key=KEY, now=NOW), expected_grant())
            self.assertEqual(verify_hop(token, "https://edge.cdn.example:9443/next", key=KEY,
                                        now=NOW, is_redirect=True), expected_grant())
            for boundary in boundaries:
                boundary.assert_not_called()

    def test_standalone_python_without_site_packages_or_django_can_use_grants(self):
        script = """
import builtins
import sys
assert sys.flags.no_site == 1
original_import = builtins.__import__
attempted = []
def guarded_import(name, *args, **kwargs):
    if name == 'django' or name.startswith('django.'):
        attempted.append(name)
        raise AssertionError('Django import is forbidden')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from datetime import datetime, timezone
from xhs_download.services.pipeline.grant import issue_grant, verify_grant, verify_hop
from xhs_download.services.pipeline.model import Work, Item, Kind, Variant
now = datetime(2026, 10, 26, tzinfo=timezone.utc)
variant = Variant('v', 'https://media.example/a.mp4')
work = Work('https://source.example/w', 'w', (Item('i', Kind.VIDEO, 1, (variant,)),), platform='p')
token = issue_grant(work, 'v', key=b'stdlib-test', now=now)
grant = verify_grant(token, key=b'stdlib-test', now=now)
assert (grant.url, grant.platform, grant.work_id, grant.variant_id) == (variant.url, 'p', 'w', 'v')
assert verify_hop(token, variant.url, key=b'stdlib-test', now=now) == grant
assert attempted == [] and not any(name == 'django' or name.startswith('django.') for name in sys.modules)
print('grant-stdlib-ok')
"""
        env = dict(os.environ)
        env.pop("DJANGO_SETTINGS_MODULE", None)
        result = subprocess.run([sys.executable, "-S", "-B", "-c", script],
                                cwd=Path(__file__).resolve().parents[2], env=env,
                                capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "grant-stdlib-ok")

    def test_ip_literals_and_numeric_address_spellings_cannot_be_issued(self):
        for host in IP_HOSTS:
            with self.subTest(host=host):
                work = work_with(media(url=f"https://{host}/clip.mp4", host_policy=()))
                self.assertEqual(validate_work(work), ())
                self.assert_rejected(issue_grant, work, "v-main", key=KEY, now=NOW)

    def test_ip_literals_and_numeric_address_spellings_cannot_be_redirected_to(self):
        token = signed_payload(host_policy=[])
        self.assertEqual(verify_grant(token, key=KEY, now=NOW), expected_grant(host_policy=()))
        for host in IP_HOSTS:
            with self.subTest(host=host):
                self.assert_rejected(verify_hop, token, f"https://{host}/clip.mp4",
                                     key=KEY, now=NOW, is_redirect=True)
