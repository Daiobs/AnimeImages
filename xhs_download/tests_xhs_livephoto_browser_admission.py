from dataclasses import FrozenInstanceError

from django.test import SimpleTestCase

from .services.platforms.xhs_livephoto_browser_admission import (
    XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION,
    XHS_LIVEPHOTO_BROWSER_VERIFIED_HOST_CLASSES,
    XHS_LIVEPHOTO_DISCOVERED_HOST_CLASSES,
    XhsLivePhotoBrowserAdmissionError,
    XhsLivePhotoBrowserAdmissionEvidenceV1,
    XhsPrivateHttpsMediaCandidateV1,
    classify_xhs_livephoto_discovered_host,
    normalize_xhs_livephoto_candidate_https,
)
from .services.platforms.xhs_livephoto_candidate_locator import (
    XhsLivePhotoMemberLocatorV1,
    XhsPrivateMediaCandidateV1,
)


PRIVATE_MARKER = "private-signed-candidate-marker"


def media_url(
    scheme,
    host,
    *,
    path="resource/path",
    query="token=alpha%2Bbeta&order=2%2F1",
    fragment="",
):
    value = f"{scheme}" + "://" + host + "/" + path
    if query is not None:
        value += "?" + query
    if fragment:
        value += "#" + fragment
    return value


def locator(role):
    if role == "still":
        return XhsLivePhotoMemberLocatorV1(
            source_item_ordinal=0,
            member_role="still",
            source_kind="url_default",
        )
    return XhsLivePhotoMemberLocatorV1(
        source_item_ordinal=0,
        member_role="motion",
        source_kind="master_url",
        codec_family_ordinal=0,
        codec_family_class="avc",
        stream_variant_index=0,
    )


def private_candidate(value, role):
    return XhsPrivateMediaCandidateV1(
        value=value,
        locator=locator(role),
        member_role=role,
    )


def normalize(value, role):
    return normalize_xhs_livephoto_candidate_https(
        private_candidate(value, role),
        member_role=role,
    )


class XhsLivePhotoBrowserAdmissionTests(SimpleTestCase):
    def assert_admission_error(self, code, callback):
        with self.assertRaises(XhsLivePhotoBrowserAdmissionError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(PRIVATE_MARKER, str(raised.exception))

    def test_reviewed_http_still_is_upgraded_to_https(self):
        result = normalize(
            media_url("http", "sns-webpic-qc.xhscdn.com"),
            "still",
        )

        self.assertTrue(result.value.startswith("https" + "://"))
        self.assertEqual(result.host_class, "still_exact")
        self.assertTrue(result.upgrade_applied)

    def test_discovered_and_browser_verified_host_sets_are_distinct(self):
        self.assertEqual(
            XHS_LIVEPHOTO_DISCOVERED_HOST_CLASSES,
            frozenset(
                {
                    "still_exact",
                    "motion_versioned",
                    "motion_zl",
                    "motion_backup",
                }
            ),
        )
        self.assertEqual(
            XHS_LIVEPHOTO_BROWSER_VERIFIED_HOST_CLASSES,
            frozenset(
                {"still_exact", "motion_versioned", "motion_backup"}
            ),
        )

    def test_discovered_classifier_recognizes_motion_zl_without_admitting_it(self):
        self.assertEqual(
            classify_xhs_livephoto_discovered_host(
                "sns-video-zl.xhscdn.com",
                member_role="motion",
            ),
            "motion_zl",
        )

    def test_browser_verified_http_motion_families_are_upgraded(self):
        cases = (
            ("sns-video-v7.xhscdn.com", "motion_versioned"),
            ("sns-bak-v12.xhscdn.com", "motion_backup"),
        )

        for host, host_class in cases:
            with self.subTest(host_class=host_class):
                result = normalize(media_url("http", host), "motion")
                self.assertEqual(result.host_class, host_class)
                self.assertTrue(result.upgrade_applied)

    def test_motion_zl_browser_admission_is_rejected(self):
        self.assert_admission_error(
            "XHS_LIVEPHOTO_BROWSER_HOST_CLASS_UNVERIFIED",
            lambda: normalize(
                media_url("http", "sns-video-zl.xhscdn.com"),
                "motion",
            ),
        )

    def test_reviewed_https_remains_https_without_upgrade(self):
        source = media_url("https", "sns-video-v3.xhscdn.com")

        result = normalize(source, "motion")

        self.assertEqual(result.value, source)
        self.assertFalse(result.upgrade_applied)

    def test_query_bytes_and_order_are_preserved_exactly(self):
        query = "z=1%2F2&a=%2B%20&empty=&repeat=1&repeat=2"
        result = normalize(
            media_url(
                "http",
                "sns-webpic-qc.xhscdn.com",
                query=query,
            ),
            "still",
        )

        self.assertEqual(result.value.split("?", 1)[1], query)

    def test_fragment_is_removed(self):
        result = normalize(
            media_url(
                "http",
                "sns-video-v8.xhscdn.com",
                fragment="client-only",
            ),
            "motion",
        )

        self.assertNotIn("#", result.value)

    def test_explicit_http_port_80_upgrades_to_default_https_port(self):
        result = normalize(
            media_url("http", "sns-video-v8.xhscdn.com:80"),
            "motion",
        )

        self.assertNotIn(":80/", result.value)
        self.assertTrue(result.value.startswith("https" + "://"))

    def test_explicit_https_port_443_is_preserved(self):
        source = media_url("https", "sns-video-v8.xhscdn.com:443")

        result = normalize(source, "motion")

        self.assertEqual(result.value, source)

    def test_protocol_relative_and_other_schemes_are_rejected(self):
        self.assert_admission_error(
            "XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID",
            lambda: normalize("//sns-webpic-qc.xhscdn.com/resource", "still"),
        )
        self.assert_admission_error(
            "XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEME_REJECTED",
            lambda: normalize(
                media_url("ftp", "sns-webpic-qc.xhscdn.com"),
                "still",
            ),
        )

    def test_userinfo_and_password_are_rejected(self):
        for authority in (
            "user@sns-video-v1.xhscdn.com",
            "user:secret@sns-video-v1.xhscdn.com",
        ):
            with self.subTest(authority=authority.split("@", 1)[0]):
                self.assert_admission_error(
                    "XHS_LIVEPHOTO_BROWSER_ADMISSION_USERINFO_REJECTED",
                    lambda authority=authority: normalize(
                        media_url("https", authority),
                        "motion",
                    ),
                )

    def test_non_default_ports_are_rejected(self):
        for scheme, host in (
            ("http", "sns-video-v1.xhscdn.com:8080"),
            ("https", "sns-video-v1.xhscdn.com:444"),
        ):
            with self.subTest(scheme=scheme):
                self.assert_admission_error(
                    "XHS_LIVEPHOTO_BROWSER_ADMISSION_PORT_REJECTED",
                    lambda scheme=scheme, host=host: normalize(
                        media_url(scheme, host),
                        "motion",
                    ),
                )

    def test_host_lookalikes_and_unrelated_xhscdn_hosts_are_rejected(self):
        hosts = (
            "sns-webpic-qc.xhscdn.com.evil.example",
            "evil-sns-video-v1.xhscdn.com",
            "sns-video-vx.xhscdn.com",
            "sns-bak-v1.xhscdn.com.evil.example",
            "sns-video-bd.xhscdn.com",
            "other.xhscdn.com",
        )

        for host in hosts:
            with self.subTest(host=host):
                self.assert_admission_error(
                    "XHS_LIVEPHOTO_BROWSER_ADMISSION_HOST_REJECTED",
                    lambda host=host: normalize(media_url("https", host), "motion"),
                )

    def test_ip_localhost_and_trailing_dot_are_rejected(self):
        hosts = (
            "127.0.0.1",
            "[::1]",
            "localhost",
            "media.localhost",
            "sns-video-v1.xhscdn.com.",
        )

        for host in hosts:
            with self.subTest(host=host):
                self.assert_admission_error(
                    "XHS_LIVEPHOTO_BROWSER_ADMISSION_HOST_REJECTED",
                    lambda host=host: normalize(media_url("https", host), "motion"),
                )

    def test_empty_root_and_backslash_paths_are_rejected(self):
        for path in ("", "\\resource"):
            with self.subTest(path=path):
                self.assert_admission_error(
                    "XHS_LIVEPHOTO_BROWSER_ADMISSION_PATH_INVALID",
                    lambda path=path: normalize(
                        media_url(
                            "https",
                            "sns-webpic-qc.xhscdn.com",
                            path=path,
                        ),
                        "still",
                    ),
                )

    def test_member_role_mismatch_is_rejected(self):
        candidate = private_candidate(
            media_url("https", "sns-webpic-qc.xhscdn.com"),
            "still",
        )

        self.assert_admission_error(
            "XHS_LIVEPHOTO_BROWSER_ADMISSION_ROLE_MISMATCH",
            lambda: normalize_xhs_livephoto_candidate_https(
                candidate,
                member_role="motion",
            ),
        )

    def test_private_value_is_absent_from_repr_equality_hash_and_structure(self):
        first = normalize(
            media_url(
                "https",
                "sns-webpic-qc.xhscdn.com",
                query="marker=" + PRIVATE_MARKER,
            ),
            "still",
        )
        second = normalize(
            media_url(
                "https",
                "sns-webpic-qc.xhscdn.com",
                query="marker=another-private-value",
            ),
            "still",
        )

        self.assertNotIn(PRIVATE_MARKER, repr(first))
        self.assertNotIn(PRIVATE_MARKER, str(first.to_structural_dict()))
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_private_https_candidate_is_frozen_and_self_validating(self):
        result = normalize(
            media_url("https", "sns-webpic-qc.xhscdn.com"),
            "still",
        )

        with self.assertRaises(FrozenInstanceError):
            result.host_class = "motion_zl"
        self.assert_admission_error(
            "XHS_LIVEPHOTO_BROWSER_ADMISSION_HOST_REJECTED",
            lambda: XhsPrivateHttpsMediaCandidateV1(
                value=media_url("https", "evil.example"),
                member_role="still",
                host_class="still_exact",
                upgrade_applied=False,
            ),
        )

    def test_motion_zl_cannot_construct_private_https_candidate(self):
        self.assert_admission_error(
            "XHS_LIVEPHOTO_BROWSER_HOST_CLASS_UNVERIFIED",
            lambda: XhsPrivateHttpsMediaCandidateV1(
                value=media_url("https", "sns-video-zl.xhscdn.com"),
                member_role="motion",
                host_class="motion_zl",
                upgrade_applied=False,
            ),
        )

    def test_unknown_hosts_are_rejected_before_network_callback(self):
        network_request_count = 0

        def admit_then_request(value):
            nonlocal network_request_count
            result = normalize(value, "motion")
            network_request_count += 1
            return result

        for host in (
            "sns-video-vx.xhscdn.com",
            "sns-bak-v1.xhscdn.com.evil.example",
            "unrelated.example",
        ):
            with self.assertRaises(XhsLivePhotoBrowserAdmissionError):
                admit_then_request(media_url("https", host))
        self.assertEqual(network_request_count, 0)

    def test_structural_browser_evidence_is_closed_and_url_free(self):
        evidence = XhsLivePhotoBrowserAdmissionEvidenceV1(
            member_role="motion",
            host_class="motion_versioned",
            format_class="mp4",
            response_type="cors",
            body_readable=True,
            final_host_class="motion_versioned",
            range_result="partial_206",
            redirect_class="none",
            upgrade_applied=True,
        )

        structural = evidence.to_structural_dict()
        self.assertEqual(
            structural["schema_version"],
            XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION,
        )
        rendered = str(structural).lower()
        self.assertNotIn("://", rendered)
        self.assertNotIn("query", rendered)
        self.assertNotIn("path", rendered)

    def test_magic_proven_jpeg_still_evidence_is_supported(self):
        evidence = XhsLivePhotoBrowserAdmissionEvidenceV1(
            member_role="still",
            host_class="still_exact",
            format_class="jpeg",
            response_type="cors",
            body_readable=True,
            final_host_class="still_exact",
            range_result="ignored_bounded_200",
            redirect_class="none",
            upgrade_applied=True,
        )

        self.assertEqual(evidence.format_class, "jpeg")

    def test_opaque_or_unreadable_browser_evidence_fails_closed(self):
        for response_type, body_readable in (("opaque", True), ("cors", False)):
            with self.subTest(response_type=response_type):
                self.assert_admission_error(
                    "XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID",
                    lambda response_type=response_type, body_readable=body_readable: (
                        XhsLivePhotoBrowserAdmissionEvidenceV1(
                            member_role="still",
                            host_class="still_exact",
                            format_class="webp",
                            response_type=response_type,
                            body_readable=body_readable,
                            final_host_class="still_exact",
                            range_result="ignored_bounded_200",
                            redirect_class="none",
                            upgrade_applied=True,
                        )
                    ),
                )

    def test_unknown_final_host_class_fails_closed(self):
        self.assert_admission_error(
            "XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID",
            lambda: XhsLivePhotoBrowserAdmissionEvidenceV1(
                member_role="motion",
                host_class="motion_versioned",
                format_class="mp4",
                response_type="cors",
                body_readable=True,
                final_host_class="motion_backup",
                range_result="partial_206",
                redirect_class="none",
                upgrade_applied=True,
            ),
        )

    def test_evidence_member_role_and_host_class_must_match(self):
        self.assert_admission_error(
            "XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID",
            lambda: XhsLivePhotoBrowserAdmissionEvidenceV1(
                member_role="still",
                host_class="motion_versioned",
                format_class="jpeg",
                response_type="cors",
                body_readable=True,
                final_host_class="motion_versioned",
                range_result="ignored_bounded_200",
                redirect_class="none",
                upgrade_applied=True,
            ),
        )

    def test_motion_zl_cannot_construct_browser_admission_evidence(self):
        self.assert_admission_error(
            "XHS_LIVEPHOTO_BROWSER_HOST_CLASS_UNVERIFIED",
            lambda: XhsLivePhotoBrowserAdmissionEvidenceV1(
                member_role="motion",
                host_class="motion_zl",
                format_class="mp4",
                response_type="cors",
                body_readable=True,
                final_host_class="motion_zl",
                range_result="partial_206",
                redirect_class="none",
                upgrade_applied=True,
            ),
        )
