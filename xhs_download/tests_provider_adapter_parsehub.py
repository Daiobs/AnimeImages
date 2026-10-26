"""Synthetic raw ParseHub objects and page-state boundaries, not live evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from .services.pipeline import Failure, FailureKind, Fetchable, Kind, Tracks, Work, validate_work
from .services.provider_adapters import ProviderAdapter, RawMapper
from .services.provider_adapters.parsehub import ParseHubMapper, ParseHubXhsMapper, failure_for_exception
from .services.provider_adapters.recordings import load_recording
from .services.provider_adapters.ytdlp import YtDlpAdapter
from .tests_provider_adapters import deny_map_io


SOURCE = "https://www.douyin.com/video/123456789"
XHS = "0123456789abcdef01234567"
XHS_SOURCE = f"https://www.xiaohongshu.com/explore/{XHS}"


def obj(name, **fields):
    return {"_class": name, "fields": fields}


class ParseHubMappingTests(TestCase):
    def map(self, raw, source=SOURCE, mapper=None):
        before = deepcopy(raw)
        with deny_map_io():
            result = (mapper or ParseHubMapper()).map(raw, source)
        self.assertEqual(before, raw)
        if isinstance(result, Work):
            self.assertEqual(validate_work(result), ())
            self.assertEqual((result.provider, result.platform), ("", ""))
            for item in result.items:
                for variant in item.variants:
                    for field in VariantFields:
                        self.assertIn(f"items.{item.item_id}.variants.{variant.variant_id}.{field}", result.field_sources)
        return result

    def test_mapper_only_no_fetch_parse_or_optional_runtime_import(self):
        import sys
        for mapper in (ParseHubMapper(), ParseHubXhsMapper()):
            self.assertIsInstance(mapper, RawMapper)
            self.assertNotIsInstance(mapper, ProviderAdapter)
            self.assertFalse(hasattr(mapper, "fetch"))
        self.assertNotIn("parsehub", sys.modules)

    def test_mixed_typed_content_preserves_order_and_sparse_facts(self):
        work = self.map(obj("MultimediaParseResult", title="Synthetic work", content="description #tag",
            media=[obj("ImageRef", url="https://cdn.example/1.jpg", ext="jpg"),
                   obj("VideoRef", url="https://cdn.example/2.webm", ext="webm", width=1920, height=1080, filesize=1234),
                   obj("AniRef", url="https://cdn.example/3.mp4", ext="mp4")]))
        self.assertEqual(work.work_id, "123456789")
        self.assertEqual([i.kind for i in work.items], [Kind.IMAGE, Kind.VIDEO, Kind.VIDEO])
        self.assertEqual(work.items[1].default_variant.size_bytes, 1234)
        self.assertIs(work.items[1].default_variant.tracks, Tracks.UNKNOWN)
        self.assertEqual(work.topics, ("tag",))
        self.assertNotIn("#tag", work.description)

    def test_unknown_kind_and_normalized_rows_are_rejected(self):
        for raw in ([[3, "https://cdn.example/a.mp4", {}]], {"items": []},
                    obj("ParseResult", media=[obj("MediaRef", url="https://cdn.example/a.mp4")])):
            self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)

    def test_source_identity_conflict_and_unidentified_source_fail(self):
        raw = obj("VideoParseResult", raw_url="https://www.douyin.com/video/999", media=obj("VideoRef", url="https://cdn.example/a.mp4"))
        self.assertEqual(self.map(raw).code, "PARSEHUB_IDENTITY_MISMATCH")
        raw["fields"]["raw_url"] = ""
        self.assertEqual(self.map(raw, "https://source.example/opaque").code, "PARSEHUB_MISSING_WORK_ID")

    def test_request_derived_identity_is_not_a_provider_field(self):
        raw = obj("VideoParseResult", media=obj("VideoRef", url="https://cdn.example/a.mp4"))
        self.assertEqual(self.map(raw).field_sources["work_id"], "request:work_id")
        raw["fields"]["raw_url"] = SOURCE
        self.assertEqual(self.map(raw).field_sources["work_id"], "parsehub_generic:raw_url")

    def test_embedded_ytdlp_cannot_bypass_known_identity(self):
        info = {"id": "999", "url": "https://cdn.example/a.mp4", "ext": "mp4"}
        raw = obj("YtVideoParseResult", raw_url=SOURCE, dl=obj("YtVideoInfo", info_json=info))
        with patch.object(YtDlpAdapter, "map", wraps=YtDlpAdapter().map) as delegate:
            self.assertEqual(self.map(raw).code, "PARSEHUB_IDENTITY_MISMATCH")
            delegate.assert_not_called()
        info["id"] = "123456789"
        raw["fields"]["raw_url"] = "https://www.douyin.com/video/999"
        self.assertEqual(self.map(raw).code, "PARSEHUB_IDENTITY_MISMATCH")

    def test_native_bili_and_douyin_result_classes(self):
        for name in ("BiliVideoParseResult", "DouyinVideoParseResult", "DouyinImageParseResult",
                     "CoolapkImageParseResult", "CoolapkMultimediaParseResult", "TikTokVideoParseResult"):
            with self.subTest(name=name):
                kind = "ImageRef" if "Image" in name else "VideoRef"
                work = self.map(obj(name, media=obj(kind, url="https://cdn.example/a.mp4")))
                self.assertIsInstance(work, Work)
                self.assertIs(work.items[0].kind, Kind.IMAGE if kind == "ImageRef" else Kind.VIDEO)

    def test_richtext_subclasses_only_extract_images_keep_original_text(self):
        for name in ("RichTextParseResult", "CoolapkRichTextParseResult", "DoubanRichTextParseResult"):
            raw = obj(name, title="Raw title", content="Original content #topic",
                      markdown_content="Unrelated body ![image](/one.jpg)", media=[])
            work = self.map(raw)
            self.assertIsInstance(work, Work)
            self.assertEqual(work.description, "Original content")
            self.assertEqual(work.topics, ("topic",))
            self.assertEqual(work.title, "Raw title")
            self.assertEqual(work.items[0].default_variant.url, "https://www.douyin.com/one.jpg")
            self.assertNotIn("Unrelated", repr(work))

    def test_same_url_typed_items_keep_all_constraints_and_positions(self):
        restricted = dict(required_headers=["Origin"], ip_bound=True, drm=True,
                          segmented=True, request_method="POST", tracks="av")
        for name, markdown in (("MultimediaParseResult", ""), ("RichTextParseResult", "![](/same.mp4)")):
            raw = obj(name, raw_url=SOURCE, markdown_content=markdown, media=[
                obj("VideoRef", url="https://www.douyin.com/same.mp4", **restricted),
                obj("VideoRef", url="https://www.douyin.com/same.mp4"),
                obj("VideoRef", url="https://www.douyin.com/other.mp4"),
            ])
            work = self.map(raw)
            self.assertIsInstance(work, Work)
            self.assertEqual(len(work.items), 2 if markdown else 3)
            for item in work.items[:-1]:
                variant = item.default_variant
                self.assertEqual(variant.required_headers, ("origin",))
                self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
                self.assertIs(variant.tracks, Tracks.AV)
                self.assertTrue(variant.drm)
                self.assertTrue(variant.segmented)
                self.assertEqual(variant.request_method, "POST")
            self.assertEqual(work.items[-1].default_variant.required_headers, ())
            self.assertFalse(work.items[-1].default_variant.drm)

    def test_explicit_false_has_raw_provenance_absence_does_not(self):
        explicit = dict(drm=False, segmented=False, is_original=False, size_exact=False)
        raw = obj("MultimediaParseResult", media=[
            obj("VideoRef", url="https://cdn.example/one.mp4", size_bytes=100, **explicit),
            obj("VideoRef", url="https://cdn.example/two.mp4"),
        ])
        work = self.map(raw)
        for index, origin in ((0, "parsehub_generic"), (1, "contract:default")):
            item = work.items[index]
            for field in explicit:
                self.assertEqual(work.field_sources[f"items.{item.item_id}.variants.{item.variants[0].variant_id}.{field}"], origin)

    def test_conflicting_known_alias_tracks_and_methods_fail_closed(self):
        for left, right in (({"tracks": "av"}, {"tracks": "audio_only"}),
                            ({"method": "POST"}, {"request_method": "PUT"})):
            raw = obj("VideoParseResult", media=[
                obj("VideoRef", url="https://cdn.example/same.mp4", **left),
                obj("VideoRef", url="https://cdn.example/same.mp4", **right),
            ])
            self.assertEqual(self.map(raw).code, "PARSEHUB_CONFLICTING_MEDIA_FACTS")

    def test_explicit_expiration_and_unknown_tracks_are_not_guessed(self):
        raw = obj("VideoParseResult", media=obj("VideoRef", url="https://cdn.example/a.mp4",
                  expires_at=1900000000, tracks="unknown"))
        work = self.map(raw)
        variant = work.items[0].default_variant
        self.assertEqual(variant.expires_at, datetime.fromtimestamp(1900000000, timezone.utc))
        self.assertIs(variant.tracks, Tracks.UNKNOWN)
        for field in ("expires_at", "tracks"):
            self.assertEqual(work.field_sources[f"items.video:1.variants.video:1:1.{field}"], "parsehub_generic")

    def test_same_url_aliases_keep_earliest_expiry_without_cross_resource_leak(self):
        for name, markdown in (("MultimediaParseResult", ""), ("RichTextParseResult", "![](/same.jpg)")):
            with self.subTest(name=name):
                raw = obj(name, raw_url=SOURCE, markdown_content=markdown, media=[
                    obj("ImageRef", url="https://www.douyin.com/same.jpg", expires_at=1800000000),
                    obj("ImageRef", url="https://www.douyin.com/same.jpg", expires_at=1900000000),
                    obj("ImageRef", url="https://www.douyin.com/other.jpg", expires_at=1700000000),
                ])
                work = self.map(raw)
                for item in work.items[:-1]:
                    self.assertEqual(item.default_variant.expires_at, datetime.fromtimestamp(1800000000, timezone.utc))
                    self.assertEqual(work.field_sources[f"items.{item.item_id}.variants.{item.variants[0].variant_id}.expires_at"], "parsehub_generic")
                self.assertEqual(work.items[-1].default_variant.expires_at, datetime.fromtimestamp(1700000000, timezone.utc))

    def test_richtext_worker_order_placeholders_and_typed_tail(self):
        raw = obj("RichTextParseResult", raw_url="https://mp.weixin.qq.com/s/ARTICLE_ID",
            markdown_content="![two](/two.jpg) ![one](/one.jpg) ![again](/two.jpg) ![skip](#x) ![skip](data:image/png;base64,AA) [page](/page) <template><img src='/hidden.jpg'></template>",
            media=[obj("ImageRef", url="https://mp.weixin.qq.com/one.jpg", width=640),
                   obj("ImageRef", url="https://mp.weixin.qq.com/tail.jpg")])
        work = self.map(raw, "https://mp.weixin.qq.com/s/ARTICLE_ID")
        self.assertEqual([i.default_variant.url for i in work.items],
                         ["https://mp.weixin.qq.com/two.jpg", "https://mp.weixin.qq.com/one.jpg", "https://mp.weixin.qq.com/tail.jpg"])
        self.assertEqual(work.items[1].default_variant.width, 640)
        self.assertNotIn("<", work.description)

    def test_richtext_worker_byte_limit_and_invalid_media(self):
        raw = obj("RichTextParseResult", markdown_content="x" * (2 * 1024 * 1024 + 1),
                  media=[obj("ImageRef", url="https://cdn.example/a.jpg")])
        self.assertEqual(self.map(raw).code, "PARSEHUB_OUTPUT_LIMIT")
        for url in ("data:text/plain,no", "http://127.0.0.1/a.jpg", "http://localhost/a.jpg", "https://cdn.example:444/a.jpg"):
            raw["fields"]["markdown_content"] = f"![]({url})"
            self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)

    def test_richtext_exact_byte_limit_and_utf8_overflow(self):
        limit = 2 * 1024 * 1024
        raw = obj("RichTextParseResult", markdown_content="x" * limit,
                  media=[obj("ImageRef", url="https://cdn.example/a.jpg")])
        with patch("xhs_download.services.provider_adapters.parsehub.markdown", return_value="") as render:
            self.assertIsInstance(self.map(raw), Work)
            render.assert_called_once_with(raw["fields"]["markdown_content"])
        raw["fields"]["markdown_content"] = "\u4e2d" * (limit // 3 + 1)
        with patch("xhs_download.services.provider_adapters.parsehub.markdown") as render:
            self.assertEqual(self.map(raw).code, "PARSEHUB_OUTPUT_LIMIT")
            render.assert_not_called()

    def test_richtext_canonical_images_only_no_extensions_or_html_output(self):
        raw = obj("RichTextParseResult", raw_url="https://mp.weixin.qq.com/s/ARTICLE_ID",
                  desc="Original description", media=[],
                  markdown_content="![](/image.JPG) ![](/fake.mp4) ![](/.jpg) [link](/page.jpg) "
                  "`<img src='/code.jpg'>` <style><img src='/style.jpg'></style>")
        from xhs_download.services.provider_adapters.parsehub import markdown as real_markdown
        with patch("xhs_download.services.provider_adapters.parsehub.markdown", wraps=real_markdown) as render:
            work = self.map(raw, "https://short.example/unresolved")
        render.assert_called_once_with(raw["fields"]["markdown_content"])
        self.assertEqual(work.description, "Original description")
        self.assertEqual([i.default_variant.url for i in work.items],
                         ["https://mp.weixin.qq.com/image.JPG", "https://mp.weixin.qq.com/fake.mp4", "https://mp.weixin.qq.com/.jpg"])
        self.assertEqual([i.default_variant.container for i in work.items], ["jpg", "", ""])
        self.assertEqual(work.field_sources["items.image:1.variants.image:1:1.container"], "adapter:url_extension")
        self.assertNotIn("<img", repr(work))

    def test_required_context_and_non_get_are_facts(self):
        raw = obj("VideoParseResult", headers={"Referer": "https://source.example/"},
                  media=obj("VideoRef", url="https://cdn.example/a.mp4", required_headers=["Referer"], ip_bound=True, method="post"))
        variant = self.map(raw).items[0].variants[0]
        self.assertEqual(variant.required_headers, ("referer",))
        self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
        self.assertEqual(variant.request_method, "POST")
        self.assertFalse(variant.deliverable)
        self.assertNotIn("https://source.example/", repr(variant))

    def test_alias_headers_cannot_fill_another_observations_missing_context(self):
        missing = obj("VideoRef", url="https://cdn.example/same.mp4",
                      headers_present=True, headers={"X-Access": None})
        customary = obj("VideoRef", url="https://cdn.example/same.mp4",
                        headers={"Referer": "https://source.example/"})
        for media, expected in ((missing, Fetchable.FALSE), (customary, Fetchable.UNKNOWN)):
            work = self.map(obj("VideoParseResult", media=media))
            self.assertIs(work.items[0].default_variant.browser_fetchable, expected)
        for name, markdown in (("MultimediaParseResult", ""),
                               ("RichTextParseResult", "![](https://cdn.example/same.mp4)")):
            for reverse in (False, True):
                with self.subTest(name=name, reverse=reverse):
                    media = [missing, customary] if not reverse else [customary, missing]
                    raw = obj(name, markdown_content=markdown, media=media + [
                        obj("VideoRef", url="https://cdn.example/other.mp4",
                            headers={"Referer": "https://source.example/"}),
                    ])
                    work = self.map(raw)
                    self.assertIsInstance(work, Work)
                    self.assertEqual(len(work.items), 2 if markdown else 3)
                    for item in work.items[:-1]:
                        self.assertIs(item.default_variant.browser_fetchable, Fetchable.FALSE)
                        self.assertEqual(work.field_sources[
                            f"items.{item.item_id}.variants.{item.variants[0].variant_id}.browser_fetchable"
                        ], "parsehub_generic")
                    self.assertIs(work.items[-1].default_variant.browser_fetchable, Fetchable.UNKNOWN)

    def test_each_alias_can_use_its_applicable_parent_context(self):
        raw = obj("MultimediaParseResult", headers={"X-Access": "available-parent-context"}, media=[
            obj("VideoRef", url="https://cdn.example/same.mp4", headers_present=True, headers={"X-Other": None}),
            obj("VideoRef", url="https://cdn.example/same.mp4"),
        ])
        work = self.map(raw)
        for item in work.items:
            self.assertIs(item.default_variant.browser_fetchable, Fetchable.UNKNOWN)
            self.assertEqual(item.default_variant.required_headers, ("x-access",))

    def test_labels_drm_and_hls_preserved(self):
        raw = obj("MultimediaParseResult", media=[
            obj("VideoRef", url="https://cdn.example/1.m3u8", quality_label="1080P"),
            obj("VideoRef", url="https://cdn.example/2.mp4", quality_label="4K", drm=True),
            obj("VideoRef", url="https://cdn.example/3.mp4", quality_label="video"),
        ])
        variants = [i.variants[0] for i in self.map(raw).items]
        self.assertEqual([v.height for v in variants], [1080, None, None])
        self.assertTrue(variants[0].segmented)
        self.assertTrue(variants[1].drm)
        self.assertEqual(variants[2].display_label, "默认清晰度")

    def test_livephoto_typed_pair_not_positional_and_xhs_requires_state(self):
        raw = obj("ImageParseResult", media=[obj("LivePhotoRef", url="https://cdn.example/a.jpg", video_url="https://cdn.example/a.mp4"),
                                              obj("ImageRef", url="https://cdn.example/b.jpg")])
        work = self.map(raw)
        self.assertEqual(len(work.items), 3)
        self.assertEqual(work.groups[0].item_ids, (work.items[0].item_id, work.items[1].item_id))
        self.assertEqual(self.map(raw, XHS_SOURCE).groups, ())

    def test_richtext_only_explicit_images_not_links_or_scripts(self):
        raw = obj("RichTextParseResult", markdown_content="Body ![one](/1.jpg) [page](/else) <script><img src='/bad.jpg'></script>", media=[])
        work = self.map(raw, "https://mp.weixin.qq.com/s/ARTICLE_ID")
        self.assertEqual(len(work.items), 1)
        self.assertEqual(work.items[0].variants[0].url, "https://mp.weixin.qq.com/1.jpg")
        self.assertEqual(work.work_id, "ARTICLE_ID")

    def test_ytdlp_delegation_reuses_mapping_and_keeps_provenance(self):
        info = {"id": "123456789", "title": "Embedded", "formats": [
            {"format_id": "v", "url": "https://cdn.example/v.mp4", "ext": "mp4", "vcodec": "avc1", "acodec": "none"},
            {"format_id": "a", "url": "https://cdn.example/a.m4a", "ext": "m4a", "vcodec": "none", "acodec": "aac"},
        ]}
        info["requested_formats"] = deepcopy(info["formats"])
        expected = YtDlpAdapter().map(info, SOURCE)
        with patch.object(YtDlpAdapter, "map", wraps=YtDlpAdapter().map) as mapped:
            work = self.map(obj("YtVideoParseResult", dl=obj("YtVideoInfo", info_json=info)))
        mapped.assert_called_once_with(info, SOURCE)
        self.assertEqual(work.items, expected.items)
        self.assertTrue(work.items[0].variants[0].merge_with)
        self.assertEqual(work.field_sources["source_url"], "request")
        self.assertEqual(work.field_sources["work_id"], "parsehub_generic:dl.info_json:yt_dlp_generic")
        for key, origin in expected.field_sources.items():
            if origin.startswith(("contract:", "adapter:", "existing:")):
                self.assertEqual(work.field_sources[key], origin)

    def test_failure_mapping_does_not_expose_messages(self):
        self.assertIs(failure_for_exception(TimeoutError("private")).kind, FailureKind.TIMEOUT)
        for status, kind in ((404, FailureKind.NOT_FOUND), (412, FailureKind.UNAVAILABLE), (429, FailureKind.UNAVAILABLE)):
            error = RuntimeError("secret URL")
            error.response = SimpleNamespace(status_code=status)
            self.assertIs(failure_for_exception(error).kind, kind)
            self.assertNotIn("secret", repr(failure_for_exception(error)))


class ParseHubXhsMappingTests(ParseHubMappingTests):
    def raw(self):
        path = Path(__file__).parent / "fixtures/provider_raw/parsehub_xhs_images/synthetic_live_photo.json"
        data = load_recording(path)
        self.assertEqual(data["provenance"]["kind"], "synthetic")
        return data["raw"]

    def test_same_image_list_item_pairing_mixed_gallery(self):
        work = self.map(self.raw(), XHS_SOURCE, ParseHubXhsMapper())
        self.assertEqual([i.kind for i in work.items], [Kind.IMAGE, Kind.VIDEO, Kind.IMAGE, Kind.IMAGE, Kind.VIDEO])
        self.assertEqual([g.item_ids for g in work.groups], [("image:1", "video:2"), ("image:4", "video:5")])
        self.assertEqual(len(work.items[1].variants), 2)
        self.assertIs(work.items[1].variants[0].tracks, Tracks.UNKNOWN)
        self.assertIn("existing:XHS_MEDIA_HOST_SUFFIXES", work.field_sources.values())

    def test_absent_livephoto_marker_does_not_invent_pair(self):
        raw = self.raw()
        raw["note"]["noteDetailMap"][XHS]["note"]["imageList"][0].pop("livePhoto")
        work = self.map(raw, XHS_SOURCE, ParseHubXhsMapper())
        self.assertEqual(len(work.groups), 1)
        self.assertEqual(len(work.items), 5)

    def test_default_url_alias_retains_info_restrictions_without_leaking(self):
        raw = self.raw()
        image = raw["note"]["noteDetailMap"][XHS]["note"]["imageList"][0]
        image["infoList"] = [{"url": image["urlDefault"], "required_headers": ["Referer"],
                              "ip_bound": True, "method": "POST", "has_drm": True}]
        work = self.map(raw, XHS_SOURCE, ParseHubXhsMapper())
        variant = work.items[0].default_variant
        self.assertEqual(variant.required_headers, ("referer",))
        self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
        self.assertEqual(variant.request_method, "POST")
        self.assertTrue(variant.drm)
        self.assertEqual(len(work.items[0].variants), 1)
        for item in work.items[1:]:
            self.assertEqual(item.default_variant.required_headers, ())
            self.assertIs(item.default_variant.browser_fetchable, Fetchable.UNKNOWN)

    def test_default_url_context_does_not_leak_to_another_image_url_or_motion(self):
        raw = self.raw()
        image = raw["note"]["noteDetailMap"][XHS]["note"]["imageList"][0]
        image.update(required_headers=["Authorization"], drm=True, method="POST", ip_bound=True)
        image["infoList"] = [{"url": "https://sns-img-hw.xhscdn.com/a-other.webp"}]
        work = self.map(raw, XHS_SOURCE, ParseHubXhsMapper())
        restricted, other = work.items[0].variants
        self.assertEqual(restricted.required_headers, ("authorization",))
        self.assertTrue(restricted.drm)
        for variant in (other, work.items[1].variants[0]):
            self.assertEqual(variant.required_headers, ())
            self.assertFalse(variant.drm)
            self.assertEqual(variant.request_method, "GET")
            self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)

    def test_note_map_key_identity_has_raw_provenance(self):
        raw = self.raw()
        raw["note"]["noteDetailMap"][XHS]["note"].pop("noteId")
        work = self.map(raw, XHS_SOURCE, ParseHubXhsMapper())
        self.assertEqual(work.work_id, XHS)
        self.assertEqual(work.field_sources["work_id"], "parsehub_xhs_images:noteDetailMap")

    def test_other_notes_and_filtered_worker_output_not_input(self):
        raw = self.raw()
        raw["note"]["noteDetailMap"][XHS]["note"]["noteId"] = "other"
        self.assertIs(self.map(raw, XHS_SOURCE, ParseHubXhsMapper()).kind, FailureKind.INVALID_RESULT)
        self.assertIs(self.map({"claimed_note_id": XHS, "items": []}, XHS_SOURCE, ParseHubXhsMapper()).kind, FailureKind.INVALID_RESULT)


from .services.pipeline import Variant
VariantFields = tuple(Variant.__dataclass_fields__)
