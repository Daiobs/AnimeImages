import ast
from pathlib import Path

from django.test import SimpleTestCase

from .services.source_lifecycle import (
    SafeSourceDiagnostic,
    SourceInputKind,
    SourceLifecycleError,
    SourceParameterAction,
    SourceParameterPolicy,
    SourceResolutionV1,
    build_source_resolution,
    extract_source_url,
)


XHS_POLICY = SourceParameterPolicy(
    remove_after_parse=("xsec_token",),
    always_drop=("utm_source", "source_note_id"),
)
DOUYIN_POLICY = SourceParameterPolicy(
    retain_for_identity=("modal_id", "aweme_id"),
    always_drop=("from_tab_name", "showtab", "utm_source"),
)
BILIBILI_POLICY = SourceParameterPolicy(
    retain_for_identity=("p",),
    always_drop=("spm_id_from", "vd_source", "utm_source"),
)


class SourceParameterPolicyTests(SimpleTestCase):
    def test_xhs_parse_only_parameter_is_removed_after_parse(self):
        url = (
            "https://www.xiaohongshu.example/explore/note123"
            "?xsec_token=fictional-value&utm_source=fixture"
        )
        parse_result = XHS_POLICY.for_parse(url)
        final_result = XHS_POLICY.after_parse(parse_result.url)

        self.assertIn("xsec_token=fictional-value", parse_result.url)
        self.assertNotIn("utm_source", parse_result.url)
        self.assertNotIn("?", final_result.url)
        self.assertEqual(final_result.removed_parameter_names, ("xsec_token",))

    def test_bilibili_page_parameter_is_retained_for_identity(self):
        url = "https://www.bilibili.example/video/BVfixture?p=3&spm_id_from=fixture"
        identity_result = BILIBILI_POLICY.for_identity(url)
        self.assertEqual(identity_result.retained_parameter_names, ("p",))
        self.assertEqual(identity_result.removed_parameter_names, ("spm_id_from",))
        self.assertTrue(identity_result.url.endswith("?p=3"))

    def test_douyin_modal_id_is_identity_relevant(self):
        url = (
            "https://www.douyin.example/user/self"
            "?from_tab_name=main&modal_id=7312345678901234567&showTab=record"
        )
        result = DOUYIN_POLICY.for_identity(url)
        self.assertEqual(result.retained_parameter_names, ("modal_id",))
        self.assertEqual(
            result.removed_parameter_names,
            ("from_tab_name", "showtab"),
        )
        self.assertIn("modal_id=", result.url)

    def test_retain_for_parse_is_distinct_from_identity(self):
        policy = SourceParameterPolicy(retain_for_parse=("quality",))
        url = "https://media.example/item?quality=original"
        self.assertIn("quality=original", policy.for_parse(url).url)
        self.assertIn("quality=original", policy.after_parse(url).url)
        self.assertNotIn("?", policy.for_identity(url).url)

    def test_unknown_parameters_fail_closed_to_drop(self):
        result = SourceParameterPolicy().for_parse(
            "https://media.example/item?unknown=fictional"
        )
        self.assertNotIn("?", result.url)
        self.assertEqual(result.removed_parameter_names, ("unknown",))

    def test_action_for_returns_closed_enum(self):
        self.assertIs(
            DOUYIN_POLICY.action_for("modal_id"),
            SourceParameterAction.RETAIN_FOR_IDENTITY,
        )
        self.assertIs(
            DOUYIN_POLICY.action_for("other"),
            SourceParameterAction.ALWAYS_DROP,
        )

    def test_policy_rejects_overlapping_parameter_names(self):
        with self.assertRaises(SourceLifecycleError) as raised:
            SourceParameterPolicy(
                retain_for_parse=("p",),
                retain_for_identity=("P",),
            )
        self.assertEqual(raised.exception.code, "SOURCE_PARAMETER_POLICY_OVERLAP")

    def test_policy_rejects_non_tuple_and_invalid_name(self):
        for kwargs, expected_code in (
            ({"retain_for_parse": ["p"]}, "SOURCE_PARAMETER_COLLECTION_INVALID"),
            ({"retain_for_parse": ("bad name",)}, "SOURCE_PARAMETER_NAME_INVALID"),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(SourceLifecycleError) as raised:
                    SourceParameterPolicy(**kwargs)
                self.assertEqual(raised.exception.code, expected_code)

    def test_duplicate_query_values_preserve_order(self):
        policy = SourceParameterPolicy(retain_for_identity=("p",))
        result = policy.for_parse("https://media.example/item?p=2&p=3")
        self.assertTrue(result.url.endswith("?p=2&p=3"))
        self.assertEqual(result.retained_parameter_names, ("p",))

    def test_fragments_are_not_forwarded(self):
        result = SourceParameterPolicy().for_parse(
            "https://media.example/item#private-fragment"
        )
        self.assertNotIn("#", result.url)


class SourceResolutionTests(SimpleTestCase):
    def test_share_text_is_extracted_without_retaining_surrounding_text(self):
        extracted = extract_source_url(
            "复制内容 "
            "https://www.xiaohongshu.example/explore/note123?xsec_token=value"
            "，打开应用"
        )
        self.assertTrue(extracted.startswith("https://"))
        self.assertNotIn("复制内容", extracted)

    def test_build_resolution_uses_caller_supplied_canonical_identity(self):
        result = build_source_resolution(
            platform_id="xhs",
            input_kind=SourceInputKind.SHARE_TEXT,
            input_value=(
                "分享 https://www.xiaohongshu.example/explore/note123"
                "?xsec_token=fictional&utm_source=fixture"
            ),
            canonical_identity="xhs:note123",
            parameter_policy=XHS_POLICY,
            resolution_duration_ms=7,
        )
        self.assertEqual(result.canonical_identity, "xhs:note123")
        self.assertIn("xsec_token", result.parse_url)
        self.assertNotIn("xsec_token", result.resolved_url)
        self.assertEqual(result.retained_parameter_names, ())
        self.assertEqual(
            result.removed_parameter_names,
            ("utm_source", "xsec_token"),
        )

    def test_caller_supplied_redirect_result_is_cleaned_without_network(self):
        result = build_source_resolution(
            platform_id="douyin",
            input_kind=SourceInputKind.SHORT_URL,
            input_value="https://v.douyin.example/fixture/",
            resolved_url=(
                "https://www.douyin.example/user/self"
                "?modal_id=7312345678901234567&showTab=record"
            ),
            canonical_identity="douyin:7312345678901234567",
            parameter_policy=DOUYIN_POLICY,
            resolution_duration_ms=11,
        )
        self.assertIn("modal_id=", result.resolved_url)
        self.assertNotIn("showTab", result.resolved_url)
        self.assertEqual(result.retained_parameter_names, ("modal_id",))

    def test_safe_diagnostic_contains_names_but_not_values_or_urls(self):
        token_value = "fictional-sensitive-token-value"
        result = build_source_resolution(
            platform_id="xhs",
            input_kind=SourceInputKind.NOTE_PAGE,
            input_value=(
                "https://www.xiaohongshu.example/explore/note123"
                f"?xsec_token={token_value}"
            ),
            canonical_identity="xhs:note123",
            parameter_policy=XHS_POLICY,
        )
        rendered = repr(result.to_safe_diagnostic().to_structural_dict())
        self.assertIn("xsec_token", rendered)
        self.assertNotIn(token_value, rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn("note123", rendered)

    def test_safe_diagnostic_has_only_closed_fields(self):
        diagnostic = SourceResolutionV1(
            platform_id="bilibili",
            input_kind=SourceInputKind.VIDEO_PAGE,
            input_url="https://www.bilibili.example/video/BVfixture?p=2",
            parse_url="https://www.bilibili.example/video/BVfixture?p=2",
            resolved_url="https://www.bilibili.example/video/BVfixture?p=2",
            canonical_identity="bilibili_video:BVfixture:2",
            retained_parameter_names=("p",),
            removed_parameter_names=(),
        ).to_safe_diagnostic()
        self.assertIsInstance(diagnostic, SafeSourceDiagnostic)
        self.assertEqual(
            set(diagnostic.to_structural_dict()),
            {
                "platform_id",
                "input_kind",
                "hostname",
                "path_class",
                "canonical_identity_kind",
                "resolution_duration_ms",
                "retained_parameter_names",
                "removed_parameter_names",
            },
        )
        self.assertEqual(diagnostic.canonical_identity_kind, "bilibili_video")
        self.assertEqual(diagnostic.path_class, "video")

    def test_safe_diagnostic_rejects_unclosed_structural_values(self):
        kwargs = {
            "platform_id": "xhs",
            "input_kind": "note_page",
            "hostname": "www.xiaohongshu.example",
            "path_class": "note",
            "canonical_identity_kind": "xhs_note",
            "resolution_duration_ms": 0,
            "retained_parameter_names": (),
            "removed_parameter_names": (),
        }
        invalid_values = (
            ("hostname", "https://media.example/item"),
            ("path_class", "/explore/note_fixture"),
            ("canonical_identity_kind", "xhs_note:note_fixture"),
            ("retained_parameter_names", ("token=value",)),
        )
        for field_name, value in invalid_values:
            with self.subTest(field_name=field_name):
                with self.assertRaises(SourceLifecycleError):
                    SafeSourceDiagnostic(**{**kwargs, field_name: value})

    def test_url_hash_is_not_accepted_as_an_implicit_identity(self):
        with self.assertRaises(SourceLifecycleError) as raised:
            build_source_resolution(
                platform_id="xhs",
                input_kind=SourceInputKind.URL,
                input_value="https://www.xiaohongshu.example/explore/note123",
                canonical_identity="f" * 64,
                parameter_policy=XHS_POLICY,
            )
        self.assertEqual(
            raised.exception.code,
            "SOURCE_CANONICAL_IDENTITY_INVALID",
        )

    def test_canonical_identity_kind_can_differ_from_platform_id(self):
        result = build_source_resolution(
            platform_id="bilibili",
            input_kind=SourceInputKind.OPUS_PAGE,
            input_value="https://www.bilibili.example/opus/123456789012345678",
            canonical_identity="bilibili_opus:123456789012345678",
            parameter_policy=BILIBILI_POLICY,
        )
        self.assertEqual(
            result.to_safe_diagnostic().canonical_identity_kind,
            "bilibili_opus",
        )

    def test_private_userinfo_and_malformed_port_fail_closed(self):
        values = (
            "http://127.0.0.1/private",
            "http://10.1.2.3/private",
            "https://user@media.example/item",
            "https://media.example:bad/item",
            "https://media.example:8443/item",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(SourceLifecycleError):
                    extract_source_url(value)

    def test_missing_or_non_http_url_fails_closed(self):
        for value in ("", "plain share text", "javascript:alert(1)"):
            with self.subTest(value=value):
                with self.assertRaises(SourceLifecycleError) as raised:
                    extract_source_url(value)
                self.assertEqual(raised.exception.code, "SOURCE_URL_INVALID")

    def test_resolution_rejects_invalid_input_kind_and_duration(self):
        kwargs = {
            "platform_id": "xhs",
            "input_kind": SourceInputKind.URL,
            "input_url": "https://media.example/item",
            "parse_url": "https://media.example/item",
            "resolved_url": "https://media.example/item",
            "canonical_identity": "xhs:item",
            "retained_parameter_names": (),
            "removed_parameter_names": (),
        }
        with self.assertRaises(SourceLifecycleError) as kind_error:
            SourceResolutionV1(**{**kwargs, "input_kind": "url"})
        self.assertEqual(kind_error.exception.code, "SOURCE_INPUT_KIND_INVALID")
        with self.assertRaises(SourceLifecycleError) as duration_error:
            SourceResolutionV1(**{**kwargs, "resolution_duration_ms": -1})
        self.assertEqual(
            duration_error.exception.code,
            "SOURCE_RESOLUTION_DURATION_INVALID",
        )

    def test_runtime_module_has_no_network_or_hash_dependency(self):
        path = Path(__file__).resolve().parent / "services" / "source_lifecycle.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        function_names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_names.append(node.name)
        joined = " ".join(imports).lower()
        self.assertNotIn("request", joined)
        self.assertNotIn("httpx", joined)
        self.assertNotIn("socket", joined)
        self.assertNotIn("hashlib", joined)
        self.assertNotIn("source_key_for_url", function_names)
