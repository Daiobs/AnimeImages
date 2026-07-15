from io import StringIO
import json
from pathlib import Path
import sys
from unittest.mock import ANY, Mock, patch

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from .services.common import XhsParseError, XhsProviderTimeoutError
from .services.platforms import (
    GalleryDlGenericProvider,
    GalleryDlResultNormalizer,
    GenericThirdPartyAdapter,
    KukutoolGenericProvider,
    KukutoolResultNormalizer,
    YtDlpGenericProvider,
    YtDlpResultNormalizer,
)
from .services.platforms.external_tools import safe_subprocess_env
from .services.platforms.gallerydl_generic import run_gallerydl_json
from .services.platforms.kukutool_generic import source_key_for_url
from .services.platforms.third_party import (
    build_third_party_rollout_status,
    frontend_budget_seconds,
    get_third_party_rule,
)
from .services.platforms.ytdlp_generic import run_ytdlp_json
from .services.provider_manifest import build_provider_manifest_token
from .services.registry import parse_media_page
from .services.youtube_merge import (
    build_youtube_merge_parse_result,
    build_youtube_merge_plan,
    download_youtube_merge_to_temp_path,
    provider_manifest_allows_youtube_merge,
)
from .services.platforms.ytdlp_merge import (
    build_parse_result as build_ytdlp_merge_parse_result,
    build_ytdlp_merge_plan,
    download_to_temp_path as download_ytdlp_merge_to_temp_path,
    provider_manifest_allows_download as provider_manifest_allows_ytdlp_merge,
)


def assert_external_command_env_isolated(testcase, mock_run):
    env = mock_run.call_args.kwargs["env"]
    cwd = mock_run.call_args.kwargs["cwd"]
    base_env = safe_subprocess_env()
    testcase.assertEqual(env["PATH"], base_env["PATH"])
    testcase.assertEqual(env["LANG"], base_env["LANG"])
    testcase.assertEqual(env["PYTHONNOUSERSITE"], "1")
    testcase.assertEqual(env["YTDLP_NO_PLUGINS"], "1")
    testcase.assertEqual(env["HOME"], cwd)
    testcase.assertIn("xhs_external_tool_", cwd)
    testcase.assertNotEqual(env["HOME"], base_env["HOME"])
    testcase.assertNotIn("DJANGO_SECRET_KEY", env)


def youtube_merge_demo_payload():
    return {
        "id": "demo",
        "title": "merge candidate demo",
        "webpage_url": "https://www.youtube.com/watch?v=demo",
        "thumbnail": "https://i.ytimg.com/vi/demo/maxresdefault.jpg",
        "formats": [
            {
                "format_id": "18",
                "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=18",
                "protocol": "https",
                "ext": "mp4",
                "height": 360,
                "width": 640,
                "fps": 30,
                "tbr": 500,
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
                "filesize": 10_000_000,
                "format_note": "360p",
            },
            {
                "format_id": "137",
                "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=137",
                "protocol": "https",
                "ext": "mp4",
                "height": 1080,
                "width": 1920,
                "fps": 60,
                "tbr": 3500,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize": 80_000_000,
                "format_note": "1080p",
            },
            {
                "format_id": "140",
                "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=140",
                "protocol": "https",
                "ext": "m4a",
                "tbr": 128,
                "abr": 128,
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 5_000_000,
                "format_note": "audio",
            },
        ],
    }


def ytdlp_merge_demo_payload():
    payload = youtube_merge_demo_payload()
    payload["formats"].append({
        "format_id": "571",
        "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=571",
        "protocol": "https",
        "ext": "mp4",
        "height": 4320,
        "width": 7680,
        "fps": 60,
        "tbr": 45000,
        "vcodec": "av01.0.16M.08",
        "acodec": "none",
        "filesize": 80_000_000,
        "format_note": "4320p",
    })
    return payload


def ytdlp_merge_oversized_high_payload():
    payload = youtube_merge_demo_payload()
    payload["formats"].append({
        "format_id": "401",
        "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=401",
        "protocol": "https",
        "ext": "mp4",
        "height": 2160,
        "width": 3840,
        "fps": 30,
        "tbr": 6000,
        "vcodec": "av01.0.12M.08",
        "acodec": "none",
        "filesize": 120_000_000,
        "format_note": "2160p",
    })
    return payload


class YouTubeMergePlannerTests(SimpleTestCase):
    def test_planner_detects_video_audio_merge_candidate(self):
        plan = build_youtube_merge_plan(
            youtube_merge_demo_payload(),
            max_height=1080,
            max_file_size_bytes=100_000_000,
        )

        self.assertTrue(plan["available"])
        self.assertTrue(plan["requires_merge"])
        self.assertTrue(plan["implemented"])
        self.assertEqual(plan["reason"], "merge_candidate_available")
        self.assertEqual(plan["complete_format"]["height"], 360)
        self.assertEqual(plan["video_format"]["format_id"], "137")
        self.assertEqual(plan["audio_format"]["format_id"], "140")
        self.assertEqual(plan["estimated_size_bytes"], 85_000_000)
        self.assertEqual(
            plan["merge_format_selector"],
            "bv*[height<=1080]+ba/b[height<=1080]/b",
        )

    def test_planner_rejects_candidate_over_size_limit(self):
        plan = build_youtube_merge_plan(
            youtube_merge_demo_payload(),
            max_height=1080,
            max_file_size_bytes=50_000_000,
        )

        self.assertFalse(plan["available"])
        self.assertTrue(plan["requires_merge"])
        self.assertFalse(plan["estimated_size_within_limit"])
        self.assertEqual(plan["reason"], "estimated_size_exceeds_limit")

    def test_planner_falls_back_to_best_height_within_size_limit(self):
        plan = build_youtube_merge_plan(
            ytdlp_merge_oversized_high_payload(),
            max_height=4320,
            max_file_size_bytes=100_000_000,
        )

        self.assertTrue(plan["available"])
        self.assertEqual(plan["reason"], "merge_candidate_available")
        self.assertEqual(plan["requested_max_height"], 4320)
        self.assertEqual(plan["max_height"], 1080)
        self.assertEqual(plan["video_format"]["format_id"], "137")
        self.assertEqual(plan["estimated_size_bytes"], 85_000_000)
        self.assertEqual(
            plan["merge_format_selector"],
            "bv*[height<=1080]+ba/b[height<=1080]/b",
        )

    def test_planner_selects_4k_when_one_gib_limit_allows_it(self):
        plan = build_youtube_merge_plan(
            ytdlp_merge_oversized_high_payload(),
            max_height=4320,
            max_file_size_bytes=1024 * 1024 * 1024,
        )

        self.assertTrue(plan["available"])
        self.assertEqual(plan["reason"], "merge_candidate_available")
        self.assertEqual(plan["requested_max_height"], 4320)
        self.assertEqual(plan["max_height"], 2160)
        self.assertEqual(plan["video_format"]["format_id"], "401")
        self.assertEqual(plan["estimated_size_bytes"], 125_000_000)
        self.assertEqual(
            plan["merge_format_selector"],
            "bv*[height<=2160]+ba/b[height<=2160]/b",
        )

    def test_planner_reports_missing_audio_stream(self):
        payload = youtube_merge_demo_payload()
        payload["formats"] = [
            item for item in payload["formats"] if item["format_id"] != "140"
        ]

        plan = build_youtube_merge_plan(payload, max_height=1080)

        self.assertFalse(plan["available"])
        self.assertFalse(plan["requires_merge"])
        self.assertEqual(plan["reason"], "no_merge_candidate")

    def test_build_parse_result_returns_signed_youtube_merge_asset(self):
        result = build_youtube_merge_parse_result(
            "https://www.youtube.com/watch?v=demo",
            {
                **youtube_merge_demo_payload(),
                "thumbnail": "https://i.ytimg.com/vi/demo/maxresdefault.jpg",
            },
        )

        self.assertEqual(result.platform, "third_party")
        self.assertEqual(result.metadata["selected_provider"], "youtube_merge")
        self.assertEqual(result.metadata["source_platform"], "youtube")
        self.assertEqual(result.video_assets[0].provider, "youtube_merge")
        self.assertEqual(
            result.video_assets[0].url,
            "https://www.youtube.com/watch?v=demo",
        )
        self.assertTrue(result.video_assets[0].provider_manifest_token)
        self.assertEqual(
            result.video_assets[0].cover_url,
            "https://i.ytimg.com/vi/demo/maxresdefault.jpg",
        )
        self.assertTrue(result.video_assets[0].size_is_estimate)

    def test_youtube_merge_manifest_rejects_tampered_source_url(self):
        asset = build_youtube_merge_parse_result(
            "https://www.youtube.com/watch?v=demo",
            youtube_merge_demo_payload(),
        ).video_assets[0]
        item = asset.to_download_item()
        item["url"] = "https://example.com/watch?v=demo"

        self.assertFalse(provider_manifest_allows_youtube_merge(item))

    @override_settings(
        YTDLP_BINARY="python -m yt_dlp",
        YOUTUBE_MERGE_FFMPEG_BINARY="/usr/bin/ffmpeg",
        YOUTUBE_MERGE_TIMEOUT_SECONDS=5,
        YOUTUBE_MERGE_MAX_FILE_SIZE_BYTES=100_000_000,
    )
    @patch("xhs_download.services.youtube_merge.run_external_command")
    def test_download_youtube_merge_invokes_ytdlp_merge_command(self, mock_run):
        asset = build_youtube_merge_parse_result(
            "https://www.youtube.com/watch?v=demo",
            youtube_merge_demo_payload(),
        ).video_assets[0]

        def fake_run(args, **_kwargs):
            output_template = args[args.index("-o") + 1]
            output_path = Path(str(output_template).replace("%(ext)s", "mp4"))
            output_path.write_bytes(b"merged-video")
            return Mock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run
        progress_events = []
        item = asset.to_download_item()
        item["size_bytes"] = 85_000_000

        temp_result = download_youtube_merge_to_temp_path(
            item,
            progress_callback=lambda completed, total: progress_events.append((completed, total)),
        )

        try:
            self.assertEqual(temp_result["content_type"], "video/mp4")
            self.assertEqual(Path(temp_result["path"]).read_bytes(), b"merged-video")
            args = mock_run.call_args.args[0]
            kwargs = mock_run.call_args.kwargs
            self.assertIn("-f", args)
            self.assertIn("bv*[height<=1080]+ba/b[height<=1080]/b", args)
            self.assertIn("--merge-output-format", args)
            self.assertIn("mp4", args)
            self.assertIn("--ffmpeg-location", args)
            self.assertTrue(progress_events)
            self.assertIsNotNone(kwargs["progress_callback"])
            self.assertTrue(kwargs["progress_path"])
            self.assertGreater(kwargs["estimated_size"], 0)
        finally:
            Path(temp_result["path"]).unlink(missing_ok=True)

    @patch("xhs_download.management.commands.inspect_youtube_merge.run_ytdlp_json")
    def test_inspect_youtube_merge_redacts_stream_urls_by_default(self, mock_run):
        mock_run.return_value = youtube_merge_demo_payload()
        output = StringIO()

        call_command(
            "inspect_youtube_merge",
            "https://www.youtube.com/watch?v=demo",
            binary=f"{sys.executable} -m yt_dlp",
            timeout=2,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["urls_redacted"])
        self.assertEqual(payload["smoke"]["parse_duration_ms"], payload["planning_duration_ms"])
        self.assertEqual(payload["smoke"]["recommended_rollout"], "staff_only")
        self.assertTrue(payload["smoke"]["merge_candidate_available"])
        self.assertTrue(payload["smoke"]["within_frontend_budget"])
        self.assertGreaterEqual(payload["smoke"]["selected_height"], 1080)
        self.assertEqual(
            payload["plan"]["video_format"]["url"],
            "https://rr1---sn.googlevideo.com/[redacted]",
        )
        self.assertNotIn("itag=137", json.dumps(payload, ensure_ascii=False))
        mock_run.assert_called_once()

    @patch("xhs_download.management.commands.inspect_youtube_merge.run_ytdlp_json")
    def test_inspect_youtube_merge_can_show_stream_urls(self, mock_run):
        mock_run.return_value = youtube_merge_demo_payload()
        output = StringIO()

        call_command(
            "inspect_youtube_merge",
            "https://www.youtube.com/watch?v=demo",
            binary=f"{sys.executable} -m yt_dlp",
            timeout=2,
            show_urls=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["urls_redacted"])
        self.assertIn("itag=137", payload["plan"]["video_format"]["url"])

    @override_settings(YTDLP_MERGE_DEFAULT_MAX_HEIGHT=1080)
    @patch("xhs_download.management.commands.inspect_youtube_merge.run_ytdlp_json")
    def test_inspect_youtube_merge_defaults_to_1080p_planning(self, mock_run):
        mock_run.return_value = ytdlp_merge_demo_payload()
        output = StringIO()

        call_command(
            "inspect_youtube_merge",
            "https://www.youtube.com/watch?v=demo",
            binary=f"{sys.executable} -m yt_dlp",
            timeout=2,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["plan"]["max_height"], 1080)
        self.assertEqual(payload["plan"]["video_format"]["height"], 1080)

    @patch("xhs_download.management.commands.inspect_youtube_merge.run_ytdlp_json")
    def test_inspect_youtube_merge_smoke_holds_when_estimated_size_exceeds_limit(
        self,
        mock_run,
    ):
        mock_run.return_value = youtube_merge_demo_payload()
        output = StringIO()

        call_command(
            "inspect_youtube_merge",
            "https://www.youtube.com/watch?v=demo",
            binary=f"{sys.executable} -m yt_dlp",
            timeout=2,
            max_size=50_000_000,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["smoke"]["recommended_rollout"], "hold")
        self.assertIn(
            "estimated_size_exceeds_limit",
            payload["smoke"]["issues"],
        )

    @patch("xhs_download.management.commands.inspect_youtube_merge.remove_temp_download")
    @patch(
        "xhs_download.management.commands.inspect_youtube_merge."
        "download_youtube_merge_to_temp_path"
    )
    @patch("xhs_download.management.commands.inspect_youtube_merge.run_ytdlp_json")
    def test_inspect_youtube_merge_download_smoke_reports_merged_file(
        self,
        mock_run,
        mock_download,
        mock_remove,
    ):
        mock_run.return_value = youtube_merge_demo_payload()
        mock_download.return_value = {
            "path": "/tmp/youtube-merge-smoke.mp4",
            "content_type": "video/mp4",
            "size": 85_000_000,
        }
        output = StringIO()

        call_command(
            "inspect_youtube_merge",
            "https://www.youtube.com/watch?v=demo",
            binary=f"{sys.executable} -m yt_dlp",
            timeout=2,
            download_smoke=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["smoke"]["parse_duration_ms"], payload["planning_duration_ms"])
        self.assertTrue(payload["download_smoke"]["ok"])
        self.assertFalse(payload["download_smoke"]["kept"])
        self.assertEqual(payload["download_smoke"]["content_type"], "video/mp4")
        self.assertEqual(payload["download_smoke"]["size_bytes"], 85_000_000)
        self.assertFalse(payload["download_smoke"]["queue"]["queue_recommended"])
        self.assertTrue(payload["download_smoke"]["queue"]["sync_acceptable"])
        self.assertNotIn("path", payload["download_smoke"])
        mock_download.assert_called_once()
        mock_remove.assert_called_once_with("/tmp/youtube-merge-smoke.mp4")

    @override_settings(YOUTUBE_FRONTEND_TIMEOUT_SECONDS=8.0)
    @patch(
        "xhs_download.management.commands.inspect_youtube_merge.time.monotonic",
        side_effect=(0.0, 1.0, 1.0, 31.0, 31.0),
    )
    @patch("xhs_download.management.commands.inspect_youtube_merge.remove_temp_download")
    @patch(
        "xhs_download.management.commands.inspect_youtube_merge."
        "download_youtube_merge_to_temp_path"
    )
    @patch("xhs_download.management.commands.inspect_youtube_merge.run_ytdlp_json")
    def test_inspect_youtube_merge_download_smoke_recommends_queue_when_slow(
        self,
        mock_run,
        mock_download,
        mock_remove,
        _mock_monotonic,
    ):
        mock_run.return_value = youtube_merge_demo_payload()
        mock_download.return_value = {
            "path": "/tmp/youtube-merge-smoke.mp4",
            "content_type": "video/mp4",
            "size": 85_000_000,
        }
        output = StringIO()

        call_command(
            "inspect_youtube_merge",
            "https://www.youtube.com/watch?v=demo",
            binary=f"{sys.executable} -m yt_dlp",
            timeout=2,
            download_smoke=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["download_smoke"]["duration_ms"], 30_000)
        self.assertEqual(payload["download_smoke"]["queue"]["frontend_budget_ms"], 8_000)
        self.assertTrue(payload["download_smoke"]["queue"]["queue_recommended"])
        self.assertFalse(payload["download_smoke"]["queue"]["sync_acceptable"])
        self.assertEqual(
            payload["download_smoke"]["queue"]["reason"],
            "download_exceeds_frontend_budget",
        )
        mock_remove.assert_called_once_with("/tmp/youtube-merge-smoke.mp4")

    @override_settings(YOUTUBE_FRONTEND_TIMEOUT_SECONDS=8.0)
    @patch(
        "xhs_download.management.commands.inspect_youtube_merge.time.monotonic",
        side_effect=(0.0, 1.0, 1.0, 31.0, 31.0),
    )
    @patch("xhs_download.management.commands.inspect_youtube_merge.remove_temp_download")
    @patch(
        "xhs_download.management.commands.inspect_youtube_merge."
        "download_youtube_merge_to_temp_path"
    )
    @patch("xhs_download.management.commands.inspect_youtube_merge.run_ytdlp_json")
    def test_inspect_youtube_merge_text_output_shows_queue_recommendation(
        self,
        mock_run,
        mock_download,
        mock_remove,
        _mock_monotonic,
    ):
        mock_run.return_value = youtube_merge_demo_payload()
        mock_download.return_value = {
            "path": "/tmp/youtube-merge-smoke.mp4",
            "content_type": "video/mp4",
            "size": 85_000_000,
        }
        output = StringIO()

        call_command(
            "inspect_youtube_merge",
            "https://www.youtube.com/watch?v=demo",
            binary=f"{sys.executable} -m yt_dlp",
            timeout=2,
            download_smoke=True,
            stdout=output,
        )
        text = output.getvalue()

        self.assertIn("download_smoke_queue_recommended: True", text)
        self.assertIn("download_smoke_queue_reason: download_exceeds_frontend_budget", text)
        mock_download.assert_called_once()
        mock_remove.assert_called_once_with("/tmp/youtube-merge-smoke.mp4")

    @patch("xhs_download.management.commands.inspect_youtube_merge.remove_temp_download")
    @patch(
        "xhs_download.management.commands.inspect_youtube_merge."
        "download_youtube_merge_to_temp_path"
    )
    @patch("xhs_download.management.commands.inspect_youtube_merge.run_ytdlp_json")
    def test_inspect_youtube_merge_download_smoke_can_keep_file(
        self,
        mock_run,
        mock_download,
        mock_remove,
    ):
        mock_run.return_value = youtube_merge_demo_payload()
        mock_download.return_value = {
            "path": "/tmp/youtube-merge-smoke.mp4",
            "content_type": "video/mp4",
            "size": 85_000_000,
        }
        output = StringIO()

        call_command(
            "inspect_youtube_merge",
            "https://www.youtube.com/watch?v=demo",
            binary=f"{sys.executable} -m yt_dlp",
            timeout=2,
            download_smoke=True,
            keep_file=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["download_smoke"]["kept"])
        self.assertEqual(
            payload["download_smoke"]["path"],
            "/tmp/youtube-merge-smoke.mp4",
        )
        mock_download.assert_called_once()
        mock_remove.assert_not_called()

    @patch(
        "xhs_download.management.commands.inspect_youtube_merge."
        "download_youtube_merge_to_temp_path"
    )
    @patch("xhs_download.management.commands.inspect_youtube_merge.run_ytdlp_json")
    def test_inspect_youtube_merge_download_smoke_does_not_run_when_plan_holds(
        self,
        mock_run,
        mock_download,
    ):
        mock_run.return_value = youtube_merge_demo_payload()
        output = StringIO()

        call_command(
            "inspect_youtube_merge",
            "https://www.youtube.com/watch?v=demo",
            binary=f"{sys.executable} -m yt_dlp",
            timeout=2,
            max_size=50_000_000,
            download_smoke=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_type"], "CommandError")
        self.assertIn("estimated_size_exceeds_limit", payload["error"])
        mock_download.assert_not_called()


@override_settings(
    TIANTIANJIEXI_PROVIDER_ENABLED=False,
    TIANTIANJIEXI_PHONE="",
    TIANTIANJIEXI_PASSWORD="",
)
class GenericThirdPartyProviderTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_third_party_rollout_status_contract(self):
        self.assertEqual(
            build_third_party_rollout_status(
                enabled=False,
                public_access_enabled=True,
                staff_only=True,
            ),
            {
                "status": "disabled",
                "reason": "third_party_entrypoint_disabled",
            },
        )
        self.assertEqual(
            build_third_party_rollout_status(
                enabled=True,
                public_access_enabled=True,
                staff_only=True,
                issue="no_ready_provider",
            ),
            {
                "status": "hold",
                "reason": "no_ready_provider",
            },
        )
        self.assertEqual(
            build_third_party_rollout_status(
                enabled=True,
                public_access_enabled=True,
                staff_only=True,
            ),
            {
                "status": "staff_only",
                "reason": "staff_only_gate_enabled",
            },
        )
        self.assertEqual(
            build_third_party_rollout_status(
                enabled=True,
                public_access_enabled=True,
                staff_only=False,
            ),
            {
                "status": "public_candidate",
                "reason": "ready_but_requires_manual_public_approval",
            },
        )

    @override_settings(
        THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=3.0,
        YOUTUBE_FRONTEND_TIMEOUT_SECONDS=8.0,
    )
    def test_youtube_frontend_budget_is_platform_specific(self):
        youtube_rule = get_third_party_rule("youtube")
        imgur_rule = get_third_party_rule("imgur")

        self.assertEqual(frontend_budget_seconds(youtube_rule), 8.0)
        self.assertEqual(frontend_budget_seconds(imgur_rule), 3.0)

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=False)
    def test_generic_third_party_parse_reports_provider_disabled(self):
        with self.assertRaisesRegex(XhsParseError, "Provider"):
            parse_media_page("https://www.instagram.com/p/example/")

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_generic_third_party_parse_reports_no_enabled_provider(self):
        provider = Mock()
        gallerydl_provider = Mock()
        ytdlp_provider = Mock()
        adapter = GenericThirdPartyAdapter(
            provider=provider,
            gallerydl_provider=gallerydl_provider,
            ytdlp_provider=ytdlp_provider,
        )

        with self.assertLogs(
            "xhs_download.services.platforms.third_party",
            level="INFO",
        ) as logs:
            with self.assertRaisesRegex(XhsParseError, "没有启用可用 Provider"):
                adapter.parse_url("https://www.youtube.com/watch?v=demo")

        self.assertIn("third_party_provider_unavailable", "\n".join(logs.output))
        provider.parse.assert_not_called()
        gallerydl_provider.parse.assert_not_called()
        ytdlp_provider.parse.assert_not_called()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_generic_third_party_parse_returns_signed_video_asset(self):
        runner = Mock(return_value={
            "title": "demo post",
            "cover": "https://scontent.cdninstagram.com/cover.jpg",
            "videos": [
                {
                    "video_fullinfo": [
                        {
                            "url": "https://scontent.cdninstagram.com/low.mp4",
                            "size": 100,
                            "type": "540p",
                        },
                        {
                            "url": "https://scontent.cdninstagram.com/high.mp4",
                            "size": 200,
                            "type": "1080p",
                        },
                    ],
                }
            ],
        })

        result = GenericThirdPartyAdapter(runner=runner).parse_url(
            "https://www.instagram.com/p/example/"
        )

        self.assertEqual(result.platform, "third_party")
        self.assertEqual(result.post_type, "video")
        self.assertEqual(result.metadata["source_platform"], "instagram")
        self.assertEqual(result.video_assets[0].url, "https://scontent.cdninstagram.com/high.mp4")
        self.assertEqual(result.video_assets[0].provider, "kukutool_generic")
        self.assertTrue(result.video_assets[0].provider_manifest_token)
        runner.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
    )
    def test_generic_third_party_parse_falls_back_to_ytdlp_provider(self):
        provider = Mock()
        provider.parse.side_effect = RuntimeError("kukutool failed")
        ytdlp_provider = Mock()
        ytdlp_provider.parse.return_value = {
            "title": "tiktok demo",
            "thumbnail": "https://p16.tiktokcdn.com/cover.jpg",
            "formats": [
                {
                    "url": "https://v16.tiktokcdn.com/video/low.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.4d401f",
                    "acodec": "mp4a.40.2",
                    "height": 720,
                    "width": 1280,
                    "tbr": 900,
                    "filesize": 100,
                    "format_note": "720p",
                },
                {
                    "url": "https://v16.tiktokcdn.com/video/high.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 2500,
                    "filesize": 200,
                    "format_note": "1080p",
                },
            ],
        }
        adapter = GenericThirdPartyAdapter(
            provider=provider,
            ytdlp_provider=ytdlp_provider,
        )

        with self.assertLogs(
            "xhs_download.services.platforms.third_party",
            level="INFO",
        ) as logs:
            result = adapter.parse_url("https://www.tiktok.com/@demo/video/123")

        self.assertEqual(result.platform, "third_party")
        self.assertEqual(result.post_type, "video")
        self.assertEqual(result.metadata["source_provider"], "yt_dlp_generic")
        self.assertEqual(result.metadata["selected_provider"], "yt_dlp_generic")
        self.assertEqual(result.metadata["source_platform"], "tiktok")
        self.assertIn("selected_provider_duration_ms", result.metadata)
        self.assertIn("provider_total_duration_ms", result.metadata)
        self.assertGreaterEqual(
            result.metadata["provider_total_duration_ms"],
            result.metadata["selected_provider_duration_ms"],
        )
        self.assertEqual(result.metadata["provider_fallback_count"], 1)
        self.assertEqual(
            tuple(attempt["provider"] for attempt in result.metadata["provider_attempts"]),
            ("kukutool_generic", "yt_dlp_generic"),
        )
        self.assertEqual(
            tuple(attempt["status"] for attempt in result.metadata["provider_attempts"]),
            ("failed", "success"),
        )
        self.assertIn("third_party_provider_failed", "\n".join(logs.output))
        self.assertIn("third_party_provider_selected", "\n".join(logs.output))
        self.assertEqual(
            result.video_assets[0].url,
            "https://v16.tiktokcdn.com/video/high.mp4",
        )
        self.assertEqual(result.video_assets[0].provider, "yt_dlp_generic")
        self.assertEqual(result.video_assets[0].quality_label, "1080p")
        self.assertTrue(result.video_assets[0].provider_manifest_token)
        provider.parse.assert_called_once()
        ytdlp_provider.parse.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
    )
    def test_generic_third_party_logs_summary_when_all_providers_fail(self):
        provider = Mock()
        provider.parse.side_effect = RuntimeError("kukutool failed")
        ytdlp_provider = Mock()
        ytdlp_provider.parse.side_effect = RuntimeError("yt-dlp failed")
        adapter = GenericThirdPartyAdapter(
            provider=provider,
            ytdlp_provider=ytdlp_provider,
        )

        with self.assertLogs(
            "xhs_download.services.platforms.third_party",
            level="INFO",
        ) as logs:
            with self.assertRaisesRegex(
                XhsParseError,
                "YouTube 第三方解析失败",
            ) as context:
                adapter.parse_url("https://www.youtube.com/watch?v=demo")

        output = "\n".join(logs.output)
        self.assertIn("third_party_provider_failed", output)
        self.assertIn("third_party_provider_all_failed", output)
        self.assertIn("yt_dlp_generic", str(context.exception))
        self.assertIn("kukutool_generic", str(context.exception))
        ytdlp_provider.parse.assert_called_once()
        provider.parse.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
        THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=3.0,
    )
    @patch(
        "xhs_download.services.platforms.third_party.time.monotonic",
        side_effect=(0.0, 0.0, 0.0, 0.0, 3.2, 3.2),
    )
    def test_generic_third_party_stops_fallback_when_frontend_budget_exhausted(
        self,
        _mock_monotonic,
    ):
        provider = Mock()
        provider.parse.side_effect = XhsProviderTimeoutError("kukutool slow")
        ytdlp_provider = Mock()
        adapter = GenericThirdPartyAdapter(
            provider=provider,
            ytdlp_provider=ytdlp_provider,
        )

        with self.assertLogs(
            "xhs_download.services.platforms.third_party",
            level="INFO",
        ) as logs:
            with self.assertRaisesRegex(XhsProviderTimeoutError, "第三方解析超时"):
                adapter.parse_url("https://www.tiktok.com/@demo/video/123")

        self.assertIn("third_party_provider_budget_exhausted", "\n".join(logs.output))
        provider.parse.assert_called_once()
        ytdlp_provider.parse.assert_not_called()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=True,
    )
    def test_generic_third_party_can_skip_kukutool_and_use_ytdlp_provider(self):
        provider = Mock()
        ytdlp_provider = Mock()
        ytdlp_provider.parse.return_value = {
            "title": "yt demo",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?high=1",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 2500,
                    "filesize": 200,
                    "format_note": "1080p",
                },
            ],
        }
        adapter = GenericThirdPartyAdapter(
            provider=provider,
            ytdlp_provider=ytdlp_provider,
        )

        result = adapter.parse_url("https://www.youtube.com/watch?v=demo")

        self.assertEqual(result.metadata["selected_provider"], "yt_dlp_generic")
        self.assertEqual(result.metadata["provider_fallback_count"], 0)
        self.assertEqual(result.video_assets[0].provider, "yt_dlp_generic")
        provider.parse.assert_not_called()
        ytdlp_provider.parse.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
    )
    def test_generic_third_party_defaults_to_skip_kukutool_provider(self):
        provider = Mock()
        ytdlp_provider = Mock()
        ytdlp_provider.parse.return_value = {
            "title": "yt demo",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?high=1",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "height": 1080,
                    "width": 1920,
                    "tbr": 2500,
                    "filesize": 200,
                    "format_note": "1080p",
                },
            ],
        }
        adapter = GenericThirdPartyAdapter(
            provider=provider,
            ytdlp_provider=ytdlp_provider,
        )

        result = adapter.parse_url("https://www.youtube.com/watch?v=demo")

        self.assertEqual(result.metadata["selected_provider"], "yt_dlp_generic")
        self.assertEqual(result.metadata["provider_fallback_count"], 0)
        self.assertEqual(
            tuple(attempt["provider"] for attempt in result.metadata["provider_attempts"]),
            ("yt_dlp_generic",),
        )
        provider.parse.assert_not_called()
        ytdlp_provider.parse.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        YOUTUBE_MERGE_PROVIDER_ENABLED=True,
        YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=True,
        YOUTUBE_MERGE_STAFF_ONLY=False,
        YTDLP_BINARY="python -m yt_dlp",
        YOUTUBE_MERGE_FFMPEG_BINARY="/usr/bin/ffmpeg",
    )
    @patch(
        "xhs_download.services.youtube_merge.probe_external_command_version",
        return_value={
            "version_probe_ok": True,
            "version_output": "ffmpeg version 8.1",
            "error": "",
        },
    )
    @patch(
        "xhs_download.services.platforms.ytdlp_generic.probe_external_command_version",
        return_value={
            "version_probe_ok": True,
            "version_output": "2026.03.17",
            "error": "",
        },
    )
    @patch("xhs_download.services.platforms.external_tools.os.path.exists", return_value=True)
    @patch("xhs_download.services.platforms.external_tools.os.access", return_value=True)
    def test_generic_third_party_prefers_youtube_merge_when_requested(
        self,
        _mock_access,
        _mock_exists,
        _mock_ytdlp_probe,
        _mock_ffmpeg_probe,
    ):
        ytdlp_provider = Mock()
        ytdlp_provider.parse.return_value = ytdlp_merge_demo_payload()
        adapter = GenericThirdPartyAdapter(ytdlp_provider=ytdlp_provider)

        result = adapter.parse_url(
            "https://www.youtube.com/watch?v=demo",
            prefer_youtube_merge=True,
        )

        self.assertEqual(result.metadata["selected_provider"], "youtube_merge")
        self.assertEqual(result.video_assets[0].provider, "youtube_merge")
        self.assertEqual(result.video_assets[0].quality_label, "4320p 高清合并")
        ytdlp_provider.parse.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        YOUTUBE_MERGE_PROVIDER_ENABLED=True,
        YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=True,
        YOUTUBE_MERGE_STAFF_ONLY=True,
        YTDLP_BINARY="python -m yt_dlp",
        YOUTUBE_MERGE_FFMPEG_BINARY="/usr/bin/ffmpeg",
    )
    @patch(
        "xhs_download.services.youtube_merge.probe_external_command_version",
        return_value={
            "version_probe_ok": True,
            "version_output": "ffmpeg version 8.1",
            "error": "",
        },
    )
    @patch(
        "xhs_download.services.platforms.ytdlp_generic.probe_external_command_version",
        return_value={
            "version_probe_ok": True,
            "version_output": "2026.03.17",
            "error": "",
        },
    )
    @patch("xhs_download.services.platforms.external_tools.os.path.exists", return_value=True)
    @patch("xhs_download.services.platforms.external_tools.os.access", return_value=True)
    def test_generic_third_party_prefers_youtube_merge_when_staff_only_was_allowed_by_view(
        self,
        _mock_access,
        _mock_exists,
        _mock_ytdlp_probe,
        _mock_ffmpeg_probe,
    ):
        ytdlp_provider = Mock()
        ytdlp_provider.parse.return_value = ytdlp_merge_demo_payload()
        adapter = GenericThirdPartyAdapter(ytdlp_provider=ytdlp_provider)

        result = adapter.parse_url(
            "https://www.youtube.com/watch?v=demo",
            prefer_youtube_merge=True,
        )

        self.assertEqual(result.metadata["selected_provider"], "youtube_merge")
        self.assertEqual(result.video_assets[0].provider, "youtube_merge")
        ytdlp_provider.parse.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_MERGE_PROVIDER_ENABLED=True,
        YTDLP_MERGE_DEFAULT_MAX_HEIGHT=1080,
        YTDLP_MERGE_HIGH_QUALITY_MAX_HEIGHT=4320,
        YTDLP_MERGE_MAX_FILE_SIZE_BYTES=100_000_000,
    )
    def test_generic_third_party_ytdlp_defaults_to_1080p_merge_candidate(self):
        ytdlp_provider = Mock()
        ytdlp_provider.parse.return_value = ytdlp_merge_demo_payload()
        adapter = GenericThirdPartyAdapter(ytdlp_provider=ytdlp_provider)

        result = adapter.parse_url("https://www.youtube.com/watch?v=demo")

        self.assertEqual(result.metadata["selected_provider"], "yt_dlp_merge")
        self.assertEqual(result.video_assets[0].provider, "yt_dlp_merge")
        self.assertEqual(result.video_assets[0].quality_label, "1080p60 高清合并")
        self.assertTrue(result.video_assets[0].size_is_estimate)
        self.assertIn(":h1080", result.video_assets[0].asset_id)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_MERGE_PROVIDER_ENABLED=True,
        YTDLP_MERGE_DEFAULT_MAX_HEIGHT=1080,
        YTDLP_MERGE_HIGH_QUALITY_MAX_HEIGHT=4320,
        YTDLP_MERGE_MAX_FILE_SIZE_BYTES=100_000_000,
    )
    def test_ytdlp_merge_can_use_high_quality_max_height_when_requested(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )

        result = build_ytdlp_merge_parse_result(
            rule,
            "YouTube",
            "https://www.youtube.com/watch?v=demo",
            ytdlp_merge_demo_payload(),
            max_height=4320,
        )

        self.assertEqual(result.metadata["selected_provider"], "yt_dlp_merge")
        self.assertEqual(result.video_assets[0].quality_label, "4320p60 高清合并")
        self.assertIn(":h4320", result.video_assets[0].asset_id)

    @override_settings(
        YTDLP_MERGE_DEFAULT_MAX_HEIGHT=1080,
        YTDLP_MERGE_HIGH_QUALITY_MAX_HEIGHT=4320,
        YTDLP_MERGE_MAX_FILE_SIZE_BYTES=100_000_000,
    )
    def test_ytdlp_merge_high_quality_falls_back_within_size_limit(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )
        plan = build_ytdlp_merge_plan(
            ytdlp_merge_oversized_high_payload(),
            max_height=4320,
        )

        self.assertTrue(plan["available"])
        self.assertEqual(plan["requested_max_height"], 4320)
        self.assertEqual(plan["max_height"], 1080)
        self.assertEqual(plan["video_format"]["format_id"], "137")

        result = build_ytdlp_merge_parse_result(
            rule,
            "YouTube",
            "https://www.youtube.com/watch?v=demo",
            ytdlp_merge_oversized_high_payload(),
            max_height=4320,
        )

        self.assertEqual(result.video_assets[0].quality_label, "1080p60 高清合并")
        self.assertIn(":h1080", result.video_assets[0].asset_id)

    @override_settings(
        YTDLP_MERGE_DEFAULT_MAX_HEIGHT=1080,
        YTDLP_MERGE_HIGH_QUALITY_MAX_HEIGHT=4320,
        YTDLP_MERGE_MAX_FILE_SIZE_BYTES=1024 * 1024 * 1024,
    )
    def test_ytdlp_merge_high_quality_selects_4k_under_one_gib_limit(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )
        plan = build_ytdlp_merge_plan(
            ytdlp_merge_oversized_high_payload(),
            max_height=4320,
        )

        self.assertTrue(plan["available"])
        self.assertEqual(plan["requested_max_height"], 4320)
        self.assertEqual(plan["max_height"], 2160)
        self.assertEqual(plan["video_format"]["format_id"], "401")

        result = build_ytdlp_merge_parse_result(
            rule,
            "YouTube",
            "https://www.youtube.com/watch?v=demo",
            ytdlp_merge_oversized_high_payload(),
            max_height=4320,
        )

        self.assertEqual(result.video_assets[0].quality_label, "2160p30 高清合并")
        self.assertIn(":h2160", result.video_assets[0].asset_id)

    @override_settings(
        YTDLP_BINARY="python -m yt_dlp",
        YTDLP_MERGE_FFMPEG_BINARY="/usr/bin/ffmpeg",
        YTDLP_MERGE_DEFAULT_MAX_HEIGHT=1080,
        YTDLP_MERGE_HIGH_QUALITY_MAX_HEIGHT=4320,
        YTDLP_MERGE_MAX_FILE_SIZE_BYTES=100_000_000,
    )
    @patch("xhs_download.services.platforms.ytdlp_merge.run_external_command")
    def test_download_ytdlp_merge_uses_asset_height_selector(self, mock_run):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )
        asset = build_ytdlp_merge_parse_result(
            rule,
            "YouTube",
            "https://www.youtube.com/watch?v=demo",
            ytdlp_merge_demo_payload(),
        ).video_assets[0]

        def fake_run(args, **_kwargs):
            output_template = args[args.index("-o") + 1]
            output_path = Path(str(output_template).replace("%(ext)s", "mp4"))
            output_path.write_bytes(b"merged-video")
            return Mock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run
        progress_events = []
        item = asset.to_download_item()
        item["size_bytes"] = 85_000_000
        temp_result = download_ytdlp_merge_to_temp_path(
            item,
            progress_callback=lambda completed, total: progress_events.append((completed, total)),
        )

        try:
            args = mock_run.call_args.args[0]
            kwargs = mock_run.call_args.kwargs
            self.assertIn("bv*[height<=1080]+ba/b[height<=1080]/b", args)
            self.assertEqual(Path(temp_result["path"]).read_bytes(), b"merged-video")
            self.assertTrue(progress_events)
            self.assertIsNotNone(kwargs["progress_callback"])
            self.assertTrue(kwargs["progress_path"])
            self.assertGreater(kwargs["estimated_size"], 0)
        finally:
            Path(temp_result["path"]).unlink(missing_ok=True)

    def test_ytdlp_merge_cover_manifest_rejects_tampered_cover_url(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )
        asset = build_ytdlp_merge_parse_result(
            rule,
            "YouTube",
            "https://www.youtube.com/watch?v=demo",
            ytdlp_merge_demo_payload(),
        ).video_assets[0]
        cover_item = {
            **asset.to_download_item(),
            "type": "image",
            "url": asset.cover_url,
            "asset_id": f"{asset.asset_id}:cover",
        }
        cover_item["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=cover_item["asset_id"],
            provider=cover_item["provider"],
            provider_resource_id=cover_item["provider_resource_id"],
            provider_cache_key=cover_item["provider_cache_key"],
            url=cover_item["url"],
            media_type="image",
        )

        self.assertTrue(provider_manifest_allows_ytdlp_merge(cover_item))
        self.assertFalse(provider_manifest_allows_ytdlp_merge({
            **cover_item,
            "url": "https://evil.example.com/cover.jpg",
        }))

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
    )
    def test_generic_third_party_uses_youtube_provider_order(self):
        provider = Mock()
        ytdlp_provider = Mock()
        ytdlp_provider.parse.return_value = {
            "title": "ordered yt demo",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?ordered=1",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "height": 1080,
                    "filesize": 200,
                    "format_note": "1080p",
                },
            ],
        }
        adapter = GenericThirdPartyAdapter(
            provider=provider,
            ytdlp_provider=ytdlp_provider,
        )
        adapter.platform_rules = (
            *(
                rule
                for rule in adapter.platform_rules
                if rule.platform_id != "youtube"
            ),
            type(adapter.platform_rules[0])(
                platform_id="youtube",
                display_name="YouTube",
                host_suffixes=("youtube.com", "youtu.be"),
                media_types=("video",),
                provider_ids=("yt_dlp_generic", "kukutool_generic"),
                media_host_suffixes=("googlevideo.com", "ytimg.com"),
            ),
        )

        result = adapter.parse_url("https://www.youtube.com/watch?v=ordered")

        self.assertEqual(
            tuple(attempt["provider"] for attempt in result.metadata["provider_attempts"]),
            ("yt_dlp_generic",),
        )
        self.assertEqual(result.video_assets[0].provider, "yt_dlp_generic")
        ytdlp_provider.parse.assert_called_once()
        provider.parse.assert_not_called()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        GALLERYDL_PROVIDER_ENABLED=True,
    )
    def test_generic_third_party_parse_falls_back_to_gallerydl_provider(self):
        provider = Mock()
        provider.parse.side_effect = RuntimeError("kukutool failed")
        gallerydl_provider = Mock()
        gallerydl_provider.parse.return_value = [
            [
                2,
                "https://i.imgur.com/demo.jpg",
                {
                    "title": "gallery demo",
                    "filename": "demo",
                    "extension": "jpg",
                    "filesize": 1234,
                },
            ]
        ]
        adapter = GenericThirdPartyAdapter(
            provider=provider,
            gallerydl_provider=gallerydl_provider,
        )

        result = adapter.parse_url("https://imgur.com/gallery/demo")

        self.assertEqual(result.platform, "third_party")
        self.assertEqual(result.post_type, "image")
        self.assertEqual(result.metadata["source_provider"], "gallery_dl_generic")
        self.assertEqual(result.metadata["selected_provider"], "gallery_dl_generic")
        self.assertEqual(result.metadata["source_platform"], "imgur")
        self.assertEqual(result.metadata["provider_fallback_count"], 1)
        self.assertEqual(result.image_assets[0].url, "https://i.imgur.com/demo.jpg")
        self.assertEqual(result.image_assets[0].provider, "gallery_dl_generic")
        self.assertEqual(result.image_assets[0].size_bytes, 1234)
        self.assertTrue(result.image_assets[0].provider_manifest_token)
        provider.parse.assert_called_once()
        gallerydl_provider.parse.assert_called_once()

    def test_gallerydl_normalizer_maps_imgur_mp4_to_video_asset(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://imgur.com/gallery/demo"
        )

        result = GalleryDlResultNormalizer().normalize(
            rule=rule,
            platform_name="Imgur",
            source_url="https://imgur.com/gallery/demo",
            payload=[
                [
                    3,
                    "https://i.imgur.com/demo.mp4",
                    {
                        "title": "animated image",
                        "extension": "mp4",
                        "mime_type": "image/gif",
                        "size": 4567,
                    },
                ]
            ],
        )

        self.assertEqual(result.post_type, "video")
        self.assertEqual(result.video_assets[0].url, "https://i.imgur.com/demo.mp4")
        self.assertEqual(result.video_assets[0].provider, "gallery_dl_generic")
        self.assertEqual(result.video_assets[0].size_bytes, 4567)

    def test_gallerydl_normalizer_prefers_original_over_thumbnail_fields(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://imgur.com/gallery/demo"
        )

        result = GalleryDlResultNormalizer().normalize(
            rule=rule,
            platform_name="Imgur",
            source_url="https://imgur.com/gallery/demo",
            payload={
                "title": "gallery with thumbnails",
                "images": [
                    {
                        "thumbnail": {
                            "url": "https://i.imgur.com/thumb.jpg",
                            "extension": "jpg",
                            "filesize": 100,
                        },
                        "original": {
                            "url": "https://i.imgur.com/original.jpg",
                            "extension": "jpg",
                            "filesize": 2000,
                        },
                    }
                ],
            },
        )

        self.assertEqual(len(result.image_assets), 1)
        self.assertEqual(
            result.image_assets[0].url,
            "https://i.imgur.com/original.jpg",
        )
        self.assertEqual(result.image_assets[0].size_bytes, 2000)

    def test_gallerydl_normalizer_keeps_thumbnail_when_it_is_only_candidate(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://imgur.com/gallery/demo"
        )

        result = GalleryDlResultNormalizer().normalize(
            rule=rule,
            platform_name="Imgur",
            source_url="https://imgur.com/gallery/demo",
            payload={
                "title": "thumbnail only gallery",
                "thumbnail": {
                    "url": "https://i.imgur.com/thumb.jpg",
                    "extension": "jpg",
                    "filesize": 100,
                },
            },
        )

        self.assertEqual(len(result.image_assets), 1)
        self.assertEqual(result.image_assets[0].url, "https://i.imgur.com/thumb.jpg")

    def test_gallerydl_normalizer_keeps_thumbnail_only_item_in_mixed_gallery(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://imgur.com/gallery/demo"
        )

        result = GalleryDlResultNormalizer().normalize(
            rule=rule,
            platform_name="Imgur",
            source_url="https://imgur.com/gallery/demo",
            payload={
                "title": "mixed gallery",
                "images": [
                    {
                        "thumbnail": {
                            "url": "https://i.imgur.com/one-thumb.jpg",
                            "extension": "jpg",
                            "filesize": 100,
                        },
                        "original": {
                            "url": "https://i.imgur.com/one-original.jpg",
                            "extension": "jpg",
                            "filesize": 2000,
                        },
                    },
                    {
                        "thumbnail": {
                            "url": "https://i.imgur.com/two-thumb.jpg",
                            "extension": "jpg",
                            "filesize": 120,
                        },
                    },
                ],
            },
        )

        self.assertEqual(len(result.image_assets), 2)
        self.assertEqual(
            [asset.url for asset in result.image_assets],
            [
                "https://i.imgur.com/one-original.jpg",
                "https://i.imgur.com/two-thumb.jpg",
            ],
        )

    def test_gallerydl_normalizer_rejects_video_when_rule_is_image_only(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.flickr.com/photos/example/123"
        )

        with self.assertRaisesRegex(ValueError, "direct media assets"):
            GalleryDlResultNormalizer().normalize(
                rule=rule,
                platform_name="Flickr",
                source_url="https://www.flickr.com/photos/example/123",
                payload=[
                    [
                        3,
                        "https://live.staticflickr.com/demo.mp4",
                        {
                            "title": "video only",
                            "extension": "mp4",
                        },
                    ]
                ],
            )

    def test_gallerydl_normalizer_does_not_inherit_media_type_for_nested_page_url(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url("https://imgur.com/a/4ai9c")

        result = GalleryDlResultNormalizer().normalize(
            rule=rule,
            platform_name="Imgur",
            source_url="https://imgur.com/a/4ai9c",
            payload=[
                [
                    2,
                    {
                        "album": {
                            "id": "4ai9c",
                            "url": "https://imgur.com/a/4ai9c",
                        },
                        "extension": "jpg",
                        "filename": "B5PvAkF",
                        "mime_type": "image/jpeg",
                        "size": 169954,
                        "url": "https://i.imgur.com/B5PvAkF.jpg",
                    },
                ],
                [
                    3,
                    "https://i.imgur.com/B5PvAkF.jpg",
                    {
                        "album": {
                            "id": "4ai9c",
                            "url": "https://imgur.com/a/4ai9c",
                        },
                        "extension": "jpg",
                        "filename": "B5PvAkF",
                        "mime_type": "image/jpeg",
                        "size": 169954,
                    },
                ],
            ],
        )

        self.assertEqual(len(result.image_assets), 1)
        self.assertEqual(result.image_assets[0].url, "https://i.imgur.com/B5PvAkF.jpg")

    @patch("xhs_download.services.platforms.external_tools.subprocess.run")
    def test_run_gallerydl_json_ignores_user_config_and_limits_items(self, mock_run):
        def fake_run(_args, **kwargs):
            kwargs["stdout"].write(b'{"title": "gallery"}')
            return Mock(returncode=0)

        mock_run.side_effect = fake_run

        payload = run_gallerydl_json(
            "https://imgur.com/gallery/demo",
            binary="python -m gallery_dl",
            timeout_seconds=2,
            max_items=12,
        )

        args = mock_run.call_args.args[0]
        self.assertEqual(payload["title"], "gallery")
        self.assertIn("--config-ignore", args)
        self.assertIn("--no-input", args)
        self.assertIn("--no-download", args)
        self.assertIn("--range", args)
        self.assertEqual(args[args.index("--range") + 1], "1-12")
        self.assertIn("-J", args)
        assert_external_command_env_isolated(self, mock_run)

    @override_settings(THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES=8)
    @patch("xhs_download.services.platforms.external_tools.subprocess.run")
    def test_run_gallerydl_json_rejects_oversized_output(self, mock_run):
        def fake_run(_args, **kwargs):
            kwargs["stdout"].write(b"x" * 2048)
            return Mock(returncode=0)

        mock_run.side_effect = fake_run

        with self.assertRaisesRegex(RuntimeError, "output exceeded"):
            run_gallerydl_json(
                "https://imgur.com/gallery/demo",
                binary="python -m gallery_dl",
                timeout_seconds=2,
                max_items=12,
            )

    @patch("xhs_download.services.platforms.gallerydl_generic.submit_provider_task")
    def test_gallerydl_provider_reports_capacity_limit(self, mock_submit):
        mock_submit.return_value = None

        with self.assertRaisesRegex(XhsProviderTimeoutError, "队列已满"):
            GalleryDlGenericProvider(runner=Mock()).parse(
                "https://imgur.com/gallery/demo"
            )

    @override_settings(
        GALLERYDL_TIMEOUT_SECONDS=15.0,
        THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=1.25,
    )
    def test_gallerydl_provider_caps_runner_timeout_to_frontend_budget(self):
        runner = Mock(return_value={
            "title": "timeout capped gallery",
            "url": "https://i.imgur.com/timeout-capped.jpg",
            "extension": "jpg",
        })

        GalleryDlGenericProvider(runner=runner).parse(
            "https://imgur.com/gallery/timeout-capped"
        )

        self.assertEqual(runner.call_args.kwargs["timeout_seconds"], 1.25)

    @override_settings(
        GALLERYDL_PAYLOAD_CACHE_TTL_SECONDS=60,
        GALLERYDL_FAILURE_CACHE_TTL_SECONDS=30,
    )
    def test_gallerydl_provider_reuses_payload_cache(self):
        runner = Mock(return_value={
            "title": "cached gallery",
            "url": "https://i.imgur.com/demo.jpg",
            "extension": "jpg",
        })
        provider = GalleryDlGenericProvider(runner=runner)

        first = provider.parse("https://imgur.com/gallery/demo")
        second = provider.parse("https://imgur.com/gallery/demo")

        self.assertEqual(first, second)
        runner.assert_called_once()

    @override_settings(
        GALLERYDL_PAYLOAD_CACHE_TTL_SECONDS=60,
        GALLERYDL_FAILURE_CACHE_TTL_SECONDS=30,
    )
    def test_gallerydl_provider_failure_cache_short_circuits(self):
        runner = Mock(side_effect=RuntimeError("boom"))
        provider = GalleryDlGenericProvider(runner=runner)

        with self.assertRaisesRegex(RuntimeError, "boom"):
            provider.parse("https://imgur.com/gallery/fail")
        with self.assertRaisesRegex(XhsProviderTimeoutError, "最近解析失败"):
            provider.parse("https://imgur.com/gallery/fail")

        runner.assert_called_once()

    @patch("xhs_download.services.platforms.external_tools.cache.get")
    @patch("xhs_download.services.platforms.external_tools.cache.set")
    def test_gallerydl_provider_continues_when_cache_is_unavailable(
        self,
        mock_cache_set,
        mock_cache_get,
    ):
        mock_cache_get.side_effect = RuntimeError("redis unavailable")
        mock_cache_set.side_effect = RuntimeError("redis unavailable")
        runner = Mock(return_value={
            "title": "uncached gallery",
            "url": "https://i.imgur.com/demo.jpg",
            "extension": "jpg",
        })

        with patch(
            "xhs_download.services.platforms.external_tools.logger.exception"
        ) as mock_log_exception:
            result = GalleryDlGenericProvider(runner=runner).parse(
                "https://imgur.com/gallery/cache-down"
            )

        self.assertEqual(result["title"], "uncached gallery")
        runner.assert_called_once()
        self.assertGreaterEqual(mock_log_exception.call_count, 1)

    def test_ytdlp_normalizer_rejects_streaming_manifest_formats(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )

        with self.assertRaisesRegex(ValueError, "direct video assets"):
            YtDlpResultNormalizer().normalize(
                rule=rule,
                platform_name="YouTube",
                source_url="https://www.youtube.com/watch?v=demo",
                payload={
                    "title": "streaming only",
                    "formats": [
                        {
                            "url": "https://rr1---sn.googlevideo.com/playlist.m3u8",
                            "protocol": "m3u8_native",
                            "height": 1080,
                        }
                    ],
                },
            )

    def test_ytdlp_normalizer_rejects_youtube_thumbnail_as_video(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )

        with self.assertLogs(
            "xhs_download.services.platforms.kukutool_generic",
            level="WARNING",
        ) as logs:
            with self.assertRaisesRegex(ValueError, "direct video assets"):
                YtDlpResultNormalizer().normalize(
                    rule=rule,
                    platform_name="YouTube",
                    source_url="https://www.youtube.com/watch?v=demo",
                    payload={
                        "title": "thumbnail only",
                        "thumbnail": "https://i.ytimg.com/vi/demo/maxresdefault.jpg",
                        "formats": [
                            {
                                "url": "https://i.ytimg.com/vi/demo/maxresdefault.jpg",
                                "protocol": "https",
                                "ext": "jpg",
                                "vcodec": "avc1.4d401f",
                                "acodec": "mp4a.40.2",
                                "height": 720,
                            }
                        ],
                    },
                )

        self.assertIn("third_party_provider_url_rejected", "\n".join(logs.output))

    def test_ytdlp_normalizer_rejects_video_only_formats(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )

        with self.assertRaisesRegex(ValueError, "direct video assets"):
            YtDlpResultNormalizer().normalize(
                rule=rule,
                platform_name="YouTube",
                source_url="https://www.youtube.com/watch?v=demo",
                payload={
                    "title": "video only",
                    "formats": [
                        {
                            "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=137",
                            "protocol": "https",
                            "ext": "mp4",
                            "height": 1080,
                            "vcodec": "avc1.640028",
                            "acodec": "none",
                            "filesize": 5_000_000,
                        }
                    ],
                },
            )

    def test_ytdlp_normalizer_rejects_codec_ambiguous_format_entries(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )

        with self.assertRaisesRegex(ValueError, "direct video assets"):
            YtDlpResultNormalizer().normalize(
                rule=rule,
                platform_name="YouTube",
                source_url="https://www.youtube.com/watch?v=demo",
                payload={
                    "title": "ambiguous format",
                    "formats": [
                        {
                            "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=18",
                            "protocol": "https",
                            "ext": "mp4",
                            "height": 360,
                            "filesize": 1_000_000,
                        }
                    ],
                },
            )

    def test_ytdlp_normalizer_prefers_complete_video_over_larger_video_only(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )

        result = YtDlpResultNormalizer().normalize(
            rule=rule,
            platform_name="YouTube",
            source_url="https://www.youtube.com/watch?v=demo",
            payload={
                "title": "complete video",
                "formats": [
                    {
                        "url": "https://rr1---sn.googlevideo.com/videoplayback?video-only=1",
                        "protocol": "https",
                        "ext": "mp4",
                        "height": 1080,
                        "vcodec": "avc1.640028",
                        "acodec": "none",
                        "filesize": 8_000_000,
                        "format_note": "1080p",
                    },
                    {
                        "url": "https://rr1---sn.googlevideo.com/videoplayback?complete=1",
                        "protocol": "https",
                        "ext": "mp4",
                        "height": 720,
                        "vcodec": "avc1.4d401f",
                        "acodec": "mp4a.40.2",
                        "audio_ext": "none",
                        "video_ext": "mp4",
                        "filesize": 2_000_000,
                        "format_note": "720p",
                    },
                ],
            },
        )

        self.assertEqual(
            result.video_assets[0].url,
            "https://rr1---sn.googlevideo.com/videoplayback?complete=1",
        )
        self.assertEqual(result.video_assets[0].quality_label, "720p")

    def test_ytdlp_normalizer_rejects_ambiguous_top_level_video_url(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )

        with self.assertRaisesRegex(ValueError, "direct video assets"):
            YtDlpResultNormalizer().normalize(
                rule=rule,
                platform_name="YouTube",
                source_url="https://www.youtube.com/watch?v=demo",
                payload={
                    "title": "ambiguous top-level",
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=137",
                    "protocol": "https",
                    "ext": "mp4",
                    "height": 1080,
                    "filesize": 5_000_000,
                },
            )

    def test_ytdlp_normalizer_rejects_streaming_top_level_video_url(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )

        with self.assertRaisesRegex(ValueError, "direct video assets"):
            YtDlpResultNormalizer().normalize(
                rule=rule,
                platform_name="YouTube",
                source_url="https://www.youtube.com/watch?v=demo",
                payload={
                    "title": "streaming top-level",
                    "url": "https://rr1---sn.googlevideo.com/manifest.m3u8",
                    "protocol": "m3u8_native",
                    "ext": "mp4",
                    "height": 1080,
                    "width": 1920,
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "filesize": 5_000_000,
                },
            )

    def test_ytdlp_normalizer_accepts_explicit_complete_top_level_video_url(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )

        result = YtDlpResultNormalizer().normalize(
            rule=rule,
            platform_name="YouTube",
            source_url="https://www.youtube.com/watch?v=demo",
            payload={
                "title": "complete top-level",
                "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=18",
                "protocol": "https",
                "ext": "mp4",
                "height": 360,
                "width": 640,
                "tbr": 500,
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
                "filesize": 1_000_000,
                "format": "360p mp4",
            },
        )

        self.assertEqual(result.video_assets[0].url, "https://rr1---sn.googlevideo.com/videoplayback?itag=18")
        self.assertEqual(result.video_assets[0].quality_label, "360p mp4")
        self.assertFalse(result.video_assets[0].size_is_estimate)

    def test_ytdlp_normalizer_marks_filesize_approx_as_estimated(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.youtube.com/watch?v=demo"
        )

        result = YtDlpResultNormalizer().normalize(
            rule=rule,
            platform_name="YouTube",
            source_url="https://www.youtube.com/watch?v=demo",
            payload={
                "title": "approximate complete video",
                "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=18",
                "protocol": "https",
                "ext": "mp4",
                "height": 360,
                "width": 640,
                "tbr": 500,
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
                "filesize_approx": 2_800_000,
                "format": "360p mp4",
            },
        )

        self.assertEqual(result.video_assets[0].size_bytes, 2_800_000)
        self.assertTrue(result.video_assets[0].size_is_estimate)

    @patch("xhs_download.services.platforms.external_tools.subprocess.run")
    def test_run_ytdlp_json_ignores_user_config(self, mock_run):
        def fake_run(_args, **kwargs):
            kwargs["stdout"].write(b'{"title": "demo"}')
            return Mock(returncode=0)

        mock_run.side_effect = fake_run

        payload = run_ytdlp_json(
            "https://www.youtube.com/watch?v=demo",
            binary="python -m yt_dlp",
            timeout_seconds=2,
        )

        args = mock_run.call_args.args[0]
        self.assertEqual(payload["title"], "demo")
        self.assertIn("--ignore-config", args)
        self.assertIn("--dump-single-json", args)
        self.assertLess(args.index("--ignore-config"), args.index("--dump-single-json"))
        assert_external_command_env_isolated(self, mock_run)

    @override_settings(THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES=8)
    @patch("xhs_download.services.platforms.external_tools.subprocess.run")
    def test_run_ytdlp_json_rejects_oversized_output(self, mock_run):
        def fake_run(_args, **kwargs):
            kwargs["stdout"].write(b"x" * 2048)
            return Mock(returncode=0)

        mock_run.side_effect = fake_run

        with self.assertRaisesRegex(RuntimeError, "output exceeded"):
            run_ytdlp_json(
                "https://www.youtube.com/watch?v=demo",
                binary="python -m yt_dlp",
                timeout_seconds=2,
            )

    @patch("xhs_download.services.platforms.ytdlp_generic.submit_provider_task")
    def test_ytdlp_provider_reports_capacity_limit(self, mock_submit):
        mock_submit.return_value = None

        with self.assertRaisesRegex(XhsProviderTimeoutError, "队列已满"):
            YtDlpGenericProvider(runner=Mock()).parse(
                "https://www.youtube.com/watch?v=demo"
            )

    @override_settings(
        YTDLP_TIMEOUT_SECONDS=15.0,
        THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=1.25,
    )
    def test_ytdlp_provider_caps_runner_timeout_to_frontend_budget(self):
        runner = Mock(return_value={
            "title": "timeout capped video",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?timeout-capped=1",
                    "protocol": "https",
                }
            ],
        })

        YtDlpGenericProvider(runner=runner).parse(
            "https://www.youtube.com/watch?v=timeout-capped"
        )

        self.assertEqual(runner.call_args.kwargs["timeout_seconds"], 1.25)

    @override_settings(
        YTDLP_TIMEOUT_SECONDS=15.0,
        THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=1.25,
    )
    def test_ytdlp_provider_honors_platform_budget_when_supplied(self):
        runner = Mock(return_value={
            "title": "platform budget video",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?budget=8",
                    "protocol": "https",
                }
            ],
        })

        YtDlpGenericProvider(runner=runner).parse(
            "https://www.youtube.com/watch?v=platform-budget",
            timeout_seconds=8.0,
        )

        self.assertEqual(runner.call_args.kwargs["timeout_seconds"], 8.0)

    @override_settings(
        YTDLP_PAYLOAD_CACHE_TTL_SECONDS=60,
        YTDLP_FAILURE_CACHE_TTL_SECONDS=30,
    )
    def test_ytdlp_provider_reuses_payload_cache(self):
        runner = Mock(return_value={
            "title": "cached video",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=18",
                    "protocol": "https",
                }
            ],
        })
        provider = YtDlpGenericProvider(runner=runner)

        first = provider.parse("https://www.youtube.com/watch?v=cache")
        second = provider.parse("https://www.youtube.com/watch?v=cache")

        self.assertEqual(first, second)
        runner.assert_called_once()

    @override_settings(
        YTDLP_PAYLOAD_CACHE_TTL_SECONDS=60,
        YTDLP_FAILURE_CACHE_TTL_SECONDS=30,
    )
    def test_ytdlp_provider_failure_cache_short_circuits(self):
        runner = Mock(side_effect=RuntimeError("boom"))
        provider = YtDlpGenericProvider(runner=runner)

        with self.assertRaisesRegex(RuntimeError, "boom"):
            provider.parse("https://www.youtube.com/watch?v=fail")
        with self.assertRaisesRegex(XhsProviderTimeoutError, "最近解析失败"):
            provider.parse("https://www.youtube.com/watch?v=fail")

        runner.assert_called_once()

    @patch("xhs_download.services.platforms.external_tools.cache.get")
    @patch("xhs_download.services.platforms.external_tools.cache.set")
    def test_ytdlp_provider_continues_when_cache_is_unavailable(
        self,
        mock_cache_set,
        mock_cache_get,
    ):
        mock_cache_get.side_effect = RuntimeError("redis unavailable")
        mock_cache_set.side_effect = RuntimeError("redis unavailable")
        runner = Mock(return_value={
            "title": "uncached video",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=18",
                    "protocol": "https",
                }
            ],
        })

        with patch(
            "xhs_download.services.platforms.external_tools.logger.exception"
        ) as mock_log_exception:
            result = YtDlpGenericProvider(runner=runner).parse(
                "https://www.youtube.com/watch?v=cache-down"
            )

        self.assertEqual(result["title"], "uncached video")
        runner.assert_called_once()
        self.assertGreaterEqual(mock_log_exception.call_count, 1)

    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    def test_inspect_third_party_provider_command_outputs_ytdlp_json(self, mock_parse):
        mock_parse.return_value = {
            "title": "yt command demo",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=22",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.4d401f",
                    "acodec": "mp4a.40.2",
                    "height": 720,
                    "filesize": 300,
                    "format_note": "720p",
                }
            ],
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.youtube.com/watch?v=demo",
            provider="ytdlp",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "yt_dlp_generic")
        self.assertEqual(payload["source_url"], "https://www.youtube.com/watch?v=demo")
        self.assertEqual(payload["platform_id"], "youtube")
        self.assertEqual(payload["smoke"]["recommended_rollout"], "staff_only")
        self.assertEqual(payload["smoke"]["selected_provider"], "yt_dlp_generic")
        self.assertEqual(payload["smoke"]["source_platform"], "youtube")
        self.assertEqual(payload["smoke"]["asset_count"], 1)
        self.assertEqual(payload["smoke"]["media_types"], ["video"])
        self.assertTrue(payload["smoke"]["within_frontend_budget"])
        self.assertEqual(payload["smoke"]["issues"], [])
        self.assertEqual(payload["attempts"][0]["provider"], "yt_dlp_generic")
        self.assertEqual(payload["attempts"][0]["status"], "success")
        self.assertEqual(payload["result"]["metadata"]["source_platform"], "youtube")
        self.assertEqual(payload["result"]["metadata"]["selected_provider"], "yt_dlp_generic")
        self.assertEqual(payload["result"]["metadata"]["provider_fallback_count"], 0)
        self.assertEqual(payload["result"]["assets"][0]["quality_label"], "720p")
        self.assertEqual(
            payload["result"]["assets"][0]["url"],
            "https://rr1---sn.googlevideo.com/[redacted]",
        )
        self.assertTrue(payload["result"]["assets"][0]["url_redacted"])
        self.assertNotIn("provider_manifest_token", output.getvalue())
        self.assertNotIn("videoplayback?itag=22", output.getvalue())
        mock_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            timeout_seconds=ANY,
        )

    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    def test_inspect_third_party_provider_show_urls_outputs_full_url(self, mock_parse):
        media_url = "https://rr1---sn.googlevideo.com/videoplayback?itag=22"
        mock_parse.return_value = {
            "title": "yt command demo",
            "formats": [
                {
                    "url": media_url,
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.4d401f",
                    "acodec": "mp4a.40.2",
                    "height": 720,
                    "filesize": 300,
                    "format_note": "720p",
                }
            ],
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.youtube.com/watch?v=demo",
            provider="ytdlp",
            as_json=True,
            show_urls=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["assets"][0]["url"], media_url)
        self.assertFalse(payload["result"]["assets"][0]["url_redacted"])
        self.assertIn("videoplayback?itag=22", output.getvalue())

    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    def test_inspect_third_party_provider_json_redacts_error_urls(self, mock_parse):
        mock_parse.side_effect = RuntimeError(
            "extractor failed for https://rr1---sn.googlevideo.com/videoplayback?sig=secret"
        )
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.youtube.com/watch?v=demo",
            provider="ytdlp",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["source_url"], "https://www.youtube.com/watch?v=demo")
        self.assertIn(
            "https://rr1---sn.googlevideo.com/[redacted]",
            payload["errors"][0]["error"],
        )
        self.assertIn(
            "https://rr1---sn.googlevideo.com/[redacted]",
            payload["attempts"][0]["error"],
        )
        self.assertNotIn("sig=secret", output.getvalue())

    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    def test_inspect_third_party_provider_show_urls_outputs_error_urls(
        self,
        mock_parse,
    ):
        error_url = "https://rr1---sn.googlevideo.com/videoplayback?sig=secret"
        mock_parse.side_effect = RuntimeError(f"extractor failed for {error_url}")
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.youtube.com/watch?v=demo",
            provider="ytdlp",
            as_json=True,
            show_urls=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertIn(error_url, payload["errors"][0]["error"])
        self.assertIn(error_url, payload["attempts"][0]["error"])
        self.assertIn("sig=secret", output.getvalue())

    @override_settings(
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
    )
    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    @patch(
        "xhs_download.services.platforms.kukutool_generic."
        "KukutoolGenericProvider.parse"
    )
    def test_inspect_third_party_provider_json_redacts_metadata_attempt_urls(
        self,
        mock_kukutool_parse,
        mock_ytdlp_parse,
    ):
        mock_kukutool_parse.side_effect = RuntimeError(
            "blocked https://v16.tiktokcdn.com/video/high.mp4?token=secret"
        )
        mock_ytdlp_parse.return_value = {
            "title": "tiktok command demo",
            "formats": [
                {
                    "url": "https://v16.tiktokcdn.com/video/fallback.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "height": 720,
                    "filesize": 300,
                    "format_note": "720p",
                }
            ],
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.tiktok.com/@demo/video/123",
            provider="auto",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertIn(
            "https://v16.tiktokcdn.com/[redacted]",
            payload["attempts"][0]["error"],
        )
        self.assertIn(
            "https://v16.tiktokcdn.com/[redacted]",
            payload["result"]["metadata"]["provider_attempts"][0]["error"],
        )
        self.assertEqual(
            payload["result"]["metadata"]["canonical_source_url"],
            "https://www.tiktok.com/@demo/video/123",
        )
        self.assertNotIn("token=secret", output.getvalue())

    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    def test_inspect_third_party_provider_text_output_includes_attempts(self, mock_parse):
        mock_parse.return_value = {
            "title": "yt text demo",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=22",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.4d401f",
                    "acodec": "mp4a.40.2",
                    "height": 720,
                    "filesize": 300,
                    "format_note": "720p",
                }
            ],
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "Open this https://www.youtube.com/watch?v=demo ，thanks",
            provider="ytdlp",
            stdout=output,
        )

        self.assertIn("source_url: https://www.youtube.com/watch?v=demo", output.getvalue())
        self.assertIn("platform_id: youtube", output.getvalue())
        self.assertIn("attempts:", output.getvalue())
        self.assertIn("- yt_dlp_generic success", output.getvalue())
        mock_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            timeout_seconds=ANY,
        )

    @override_settings(
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
    )
    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    @patch(
        "xhs_download.services.platforms.kukutool_generic."
        "KukutoolGenericProvider.parse"
    )
    def test_inspect_third_party_provider_text_output_shows_fallback_attempts(
        self,
        mock_kukutool_parse,
        mock_ytdlp_parse,
    ):
        mock_kukutool_parse.side_effect = RuntimeError("kukutool failed")
        mock_ytdlp_parse.return_value = {
            "title": "tiktok text demo",
            "formats": [
                {
                    "url": "https://v16.tiktokcdn.com/video/high.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "height": 1080,
                    "filesize": 500,
                    "format_note": "1080p",
                }
            ],
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.tiktok.com/@demo/video/123",
            provider="auto",
            stdout=output,
        )

        command_output = output.getvalue()
        self.assertIn("provider: yt_dlp_generic", command_output)
        self.assertIn("platform_id: tiktok", command_output)
        self.assertIn("- kukutool_generic failed", command_output)
        self.assertIn("- yt_dlp_generic success", command_output)
        mock_kukutool_parse.assert_called_once_with(
            "https://www.tiktok.com/@demo/video/123",
            timeout_seconds=ANY,
        )
        mock_ytdlp_parse.assert_called_once_with(
            "https://www.tiktok.com/@demo/video/123",
            timeout_seconds=ANY,
        )

    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    def test_inspect_third_party_provider_command_extracts_share_text_url(
        self,
        mock_parse,
    ):
        mock_parse.return_value = {
            "title": "yt command demo",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=22",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.4d401f",
                    "acodec": "mp4a.40.2",
                    "height": 720,
                    "filesize": 300,
                    "format_note": "720p",
                }
            ],
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "Open this https://www.youtube.com/watch?v=demo ，thanks",
            provider="ytdlp",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source_url"], "https://www.youtube.com/watch?v=demo")
        self.assertEqual(payload["platform_id"], "youtube")
        self.assertEqual(
            payload["result"]["metadata"]["canonical_source_url"],
            "https://www.youtube.com/watch?v=demo",
        )
        mock_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            timeout_seconds=ANY,
        )

    @patch(
        "xhs_download.services.platforms.gallerydl_generic."
        "GalleryDlGenericProvider.parse"
    )
    def test_inspect_third_party_provider_command_outputs_gallerydl_json(self, mock_parse):
        mock_parse.return_value = [
            [
                2,
                "https://i.imgur.com/demo.jpg",
                {
                    "title": "gallery command demo",
                    "filename": "demo",
                    "extension": "jpg",
                },
            ]
        ]
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://imgur.com/gallery/demo",
            provider="gallerydl",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "gallery_dl_generic")
        self.assertEqual(payload["smoke"]["recommended_rollout"], "staff_only")
        self.assertEqual(payload["smoke"]["asset_count"], 1)
        self.assertEqual(payload["smoke"]["media_types"], ["image"])
        self.assertEqual(payload["result"]["metadata"]["source_platform"], "imgur")
        self.assertEqual(payload["result"]["metadata"]["selected_provider"], "gallery_dl_generic")
        self.assertEqual(payload["result"]["metadata"]["provider_fallback_count"], 0)
        self.assertEqual(payload["result"]["assets"][0]["type"], "image")
        self.assertEqual(
            payload["result"]["assets"][0]["url"],
            "https://i.imgur.com/[redacted]",
        )
        self.assertTrue(payload["result"]["assets"][0]["url_redacted"])
        self.assertNotIn("provider_manifest_token", output.getvalue())
        self.assertNotIn("demo.jpg", output.getvalue())

    @override_settings(THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=3.0)
    @patch(
        "xhs_download.management.commands.inspect_third_party_provider.time.monotonic",
        side_effect=(0.0, 0.0, 0.0, 4.0, 4.0),
    )
    @patch(
        "xhs_download.services.platforms.gallerydl_generic."
        "GalleryDlGenericProvider.parse"
    )
    def test_inspect_third_party_provider_smoke_holds_when_parse_exceeds_budget(
        self,
        mock_parse,
        _mock_monotonic,
    ):
        mock_parse.return_value = [
            [
                2,
                "https://i.imgur.com/demo.jpg",
                {
                    "title": "slow gallery command demo",
                    "filename": "demo",
                    "extension": "jpg",
                },
            ]
        ]
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://imgur.com/gallery/demo",
            provider="gallerydl",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["smoke"]["recommended_rollout"], "hold")
        self.assertFalse(payload["smoke"]["within_frontend_budget"])
        self.assertIn("parse_exceeds_frontend_budget", payload["smoke"]["issues"])

    @patch(
        "xhs_download.services.platforms.gallerydl_generic."
        "GalleryDlGenericProvider.parse"
    )
    def test_inspect_third_party_provider_timeout_overrides_frontend_budget_for_explicit_provider(
        self,
        mock_parse,
    ):
        mock_parse.return_value = [
            [
                2,
                "https://i.imgur.com/demo.jpg",
                {
                    "title": "gallery command demo",
                    "filename": "demo",
                    "extension": "jpg",
                },
            ]
        ]
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://imgur.com/gallery/demo",
            provider="gallerydl",
            timeout=8,
            as_json=True,
            stdout=output,
        )

        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.assertEqual(settings.THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS, 8)
        self.assertEqual(settings.GALLERYDL_TIMEOUT_SECONDS, 8)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=False,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False,
        YOUTUBE_MERGE_PROVIDER_ENABLED=False,
    )
    def test_inspect_third_party_provider_health_json_does_not_require_url(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["settings"]["third_party_provider_enabled"])
        self.assertFalse(payload["settings"]["third_party_staff_only"])
        self.assertEqual(payload["rollout"]["status"], "disabled")
        self.assertEqual(
            payload["rollout"]["reason"],
            "third_party_entrypoint_disabled",
        )
        self.assertEqual(
            payload["settings"]["third_party_external_output_max_bytes"],
            settings.THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES,
        )
        self.assertIn("yt_dlp_generic", payload["providers"])
        self.assertIn("gallery_dl_generic", payload["providers"])
        self.assertIn("youtube", {
            rule["platform_id"] for rule in payload["platform_rules"]
        })

    @override_settings(THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES=8)
    def test_inspect_third_party_provider_health_reports_effective_output_limit(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(
            payload["settings"]["third_party_external_output_max_bytes"],
            1024,
        )

    @override_settings(
        YTDLP_BINARY="",
        YTDLP_PROVIDER_ENABLED=False,
        YOUTUBE_MERGE_PROVIDER_ENABLED=False,
    )
    @patch("xhs_download.services.platforms.ytdlp_generic.probe_external_command_version")
    def test_inspect_third_party_provider_health_accepts_explicit_binary(self, mock_probe):
        mock_probe.return_value = {
            "version_probe_ok": True,
            "version_output": "yt-dlp test",
            "error": "",
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            provider="ytdlp",
            binary=f"{sys.executable} -m yt_dlp",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        ytdlp_health = payload["providers"]["yt_dlp_generic"]
        self.assertEqual(ytdlp_health["binary"], f"{sys.executable} -m yt_dlp")
        self.assertTrue(ytdlp_health["binary_configured"])
        self.assertTrue(ytdlp_health["executable_found"])
        self.assertEqual(ytdlp_health["resolved_executable"], sys.executable)
        self.assertTrue(ytdlp_health["version_probe_ok"])
        self.assertTrue(ytdlp_health["ready"])
        mock_probe.assert_called_once()

    @override_settings(GALLERYDL_BINARY="")
    @patch("xhs_download.services.platforms.gallerydl_generic.probe_external_command_version")
    def test_inspect_third_party_provider_health_accepts_explicit_gallerydl_binary(self, mock_probe):
        mock_probe.return_value = {
            "version_probe_ok": True,
            "version_output": "gallery-dl test",
            "error": "",
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            provider="gallerydl",
            binary=f"{sys.executable} -m gallery_dl",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        gallerydl_health = payload["providers"]["gallery_dl_generic"]
        self.assertEqual(gallerydl_health["binary"], f"{sys.executable} -m gallery_dl")
        self.assertTrue(gallerydl_health["binary_configured"])
        self.assertTrue(gallerydl_health["executable_found"])
        self.assertEqual(gallerydl_health["resolved_executable"], sys.executable)
        self.assertTrue(gallerydl_health["version_probe_ok"])
        self.assertTrue(gallerydl_health["ready"])
        mock_probe.assert_called_once()

    def test_inspect_third_party_provider_health_rejects_auto_binary(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            provider="auto",
            binary=f"{sys.executable} -m yt_dlp",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["attempts"], [])
        self.assertEqual(payload["errors"][0]["provider"], "auto")
        self.assertEqual(
            payload["errors"][0]["error_type"],
            "BinaryRequiresExplicitProvider",
        )

    @patch("xhs_download.services.platforms.kukutool_generic.cache.get")
    def test_kukutool_generic_health_reports_cache_error_without_raising(self, mock_cache_get):
        mock_cache_get.side_effect = RuntimeError("cache down")

        health = KukutoolGenericProvider().health(source_key="abc123")

        self.assertEqual(health["cache_error"], "RuntimeError")
        self.assertFalse(health["failure_cache"]["hit"])
        self.assertFalse(health["single_flight"]["active"])

    @override_settings(
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=True,
        GALLERYDL_PROVIDER_ENABLED=True,
    )
    def test_inspect_third_party_provider_health_text_includes_platform_coverage(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            stdout=output,
        )
        text = output.getvalue()

        self.assertIn("platform coverage:", text)
        self.assertIn("rollout:", text)
        self.assertIn("- YouTube (youtube):", text)
        self.assertIn("providers=yt_dlp_generic, kukutool_generic", text)
        self.assertIn("- Flickr (flickr):", text)
        self.assertIn("gallery_dl_generic", text)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_inspect_third_party_provider_health_reports_missing_enabled_provider(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["issues"][0]["code"], "no_provider_enabled")
        self.assertEqual(payload["rollout"]["status"], "hold")
        self.assertEqual(payload["rollout"]["reason"], "no_provider_enabled")
        self.assertEqual(payload["enabled_provider_ids"], [])
        self.assertEqual(payload["ready_provider_ids"], [])

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_inspect_third_party_provider_health_treats_kukutool_as_ready(self):
        cache.clear()
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["enabled_provider_ids"], ["kukutool_generic"])
        self.assertEqual(payload["ready_provider_ids"], ["kukutool_generic"])
        self.assertEqual(payload["rollout"]["status"], "public_candidate")
        self.assertEqual(
            payload["rollout"]["reason"],
            "ready_but_requires_manual_public_approval",
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_inspect_third_party_provider_health_reports_public_candidate_rollout(self):
        cache.clear()
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["rollout"]["status"], "public_candidate")
        self.assertEqual(
            payload["rollout"]["reason"],
            "ready_but_requires_manual_public_approval",
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_inspect_third_party_provider_health_ignores_legacy_staff_only_env_gate(self):
        cache.clear()
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["rollout"]["status"], "public_candidate")
        self.assertEqual(
            payload["rollout"]["reason"],
            "ready_but_requires_manual_public_approval",
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_BINARY="/path/that/does/not/exist/yt-dlp",
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_inspect_third_party_provider_health_reports_not_ready_provider(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            health=True,
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["enabled_provider_ids"], ["yt_dlp_generic"])
        self.assertEqual(payload["ready_provider_ids"], [])
        self.assertEqual(payload["issues"][0]["code"], "provider_not_ready")
        self.assertEqual(payload["issues"][0]["provider"], "yt_dlp_generic")

    def test_inspect_third_party_provider_requires_url_without_health(self):
        with self.assertRaisesRegex(CommandError, "URL is required"):
            call_command(
                "inspect_third_party_provider",
                stdout=StringIO(),
            )

    def test_inspect_third_party_provider_reports_missing_url_json(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertNotIn("source_url", payload)
        self.assertNotIn("platform_id", payload)
        self.assertEqual(payload["attempts"], [])
        self.assertEqual(payload["errors"][0]["provider"], "auto")
        self.assertEqual(payload["errors"][0]["status"], "rejected")
        self.assertEqual(payload["errors"][0]["error_type"], "MissingUrl")
        self.assertIn("URL is required", payload["errors"][0]["error"])

    def test_inspect_third_party_provider_reports_unsupported_url_json(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.xiaohongshu.com/explore/demo",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["source_url"], "https://www.xiaohongshu.com/explore/demo")
        self.assertNotIn("platform_id", payload)
        self.assertEqual(payload["attempts"], [])
        self.assertEqual(payload["errors"][0]["status"], "rejected")
        self.assertEqual(payload["errors"][0]["error_type"], "UnsupportedUrl")
        self.assertIn("not supported", payload["errors"][0]["error"])

    @override_settings(
        THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=3.0,
        THIRD_PARTY_KUKUTOOL_TIMEOUT_SECONDS=12.0,
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_TIMEOUT_SECONDS=15.0,
        GALLERYDL_TIMEOUT_SECONDS=15.0,
    )
    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    @patch(
        "xhs_download.services.platforms.kukutool_generic."
        "KukutoolGenericProvider.parse"
    )
    def test_inspect_third_party_provider_auto_timeout_applies_to_all_providers(
        self,
        mock_kukutool_parse,
        mock_ytdlp_parse,
    ):
        mock_kukutool_parse.side_effect = RuntimeError("kukutool failed")
        mock_ytdlp_parse.return_value = {
            "title": "yt command demo",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=22",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.4d401f",
                    "acodec": "mp4a.40.2",
                    "height": 720,
                    "filesize": 300,
                    "format_note": "720p",
                }
            ],
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.youtube.com/watch?v=demo",
            provider="auto",
            timeout=1.25,
            as_json=True,
            stdout=output,
        )

        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.assertEqual(settings.THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS, 1.25)
        self.assertEqual(settings.THIRD_PARTY_KUKUTOOL_TIMEOUT_SECONDS, 1.25)
        self.assertEqual(settings.YTDLP_TIMEOUT_SECONDS, 1.25)
        self.assertEqual(settings.GALLERYDL_TIMEOUT_SECONDS, 1.25)

    @override_settings(
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=True,
    )
    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    @patch(
        "xhs_download.services.platforms.kukutool_generic."
        "KukutoolGenericProvider.parse"
    )
    def test_inspect_third_party_provider_auto_respects_provider_enabled_flags(
        self,
        mock_kukutool_parse,
        mock_ytdlp_parse,
    ):
        mock_ytdlp_parse.return_value = {
            "title": "yt command demo",
            "formats": [
                {
                    "url": "https://rr1---sn.googlevideo.com/videoplayback?itag=22",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.4d401f",
                    "acodec": "mp4a.40.2",
                    "height": 720,
                    "filesize": 300,
                    "format_note": "720p",
                }
            ],
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.youtube.com/watch?v=demo",
            provider="auto",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "yt_dlp_generic")
        mock_kukutool_parse.assert_not_called()
        mock_ytdlp_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            timeout_seconds=ANY,
        )

    @override_settings(
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=True,
    )
    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse",
        side_effect=XhsProviderTimeoutError("yt-dlp timed out"),
    )
    def test_inspect_third_party_provider_json_marks_timeout_attempt(
        self,
        mock_ytdlp_parse,
    ):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.youtube.com/watch?v=demo",
            provider="auto",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["source_url"], "https://www.youtube.com/watch?v=demo")
        self.assertEqual(payload["platform_id"], "youtube")
        self.assertEqual(payload["errors"][0]["provider"], "yt_dlp_generic")
        self.assertEqual(payload["errors"][0]["status"], "timeout")
        self.assertEqual(payload["errors"][0]["error_type"], "XhsProviderTimeoutError")
        self.assertEqual(payload["smoke"]["recommended_rollout"], "hold")
        self.assertEqual(payload["smoke"]["asset_count"], 0)
        self.assertEqual(payload["smoke"]["media_types"], [])
        self.assertEqual(payload["smoke"]["source_platform"], "youtube")
        self.assertEqual(payload["smoke"]["issues"], ["provider_timeout"])
        self.assertEqual(payload["attempts"][0]["provider"], "yt_dlp_generic")
        self.assertEqual(payload["attempts"][0]["status"], "timeout")
        mock_ytdlp_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            timeout_seconds=ANY,
        )

    @override_settings(THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False)
    @patch(
        "xhs_download.services.platforms.kukutool_generic."
        "KukutoolGenericProvider.parse"
    )
    def test_inspect_third_party_provider_explicit_provider_ignores_enabled_flag(
        self,
        mock_kukutool_parse,
    ):
        mock_kukutool_parse.return_value = {
            "title": "kukutool command demo",
            "videos": ["https://hwmov.a.kwimgs.com/demo.mp4"],
        }
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.kuaishou.com/short-video/demo",
            provider="kukutool",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "kukutool_generic")
        mock_kukutool_parse.assert_called_once_with(
            "https://www.kuaishou.com/short-video/demo",
            timeout_seconds=ANY,
        )

    def test_inspect_third_party_provider_binary_requires_explicit_provider(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.youtube.com/watch?v=demo",
            provider="auto",
            binary=".venv/bin/python -m yt_dlp",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["source_url"], "https://www.youtube.com/watch?v=demo")
        self.assertEqual(payload["platform_id"], "youtube")
        self.assertEqual(payload["attempts"], [])
        self.assertEqual(payload["errors"][0]["provider"], "auto")
        self.assertEqual(payload["errors"][0]["status"], "rejected")
        self.assertEqual(
            payload["errors"][0]["error_type"],
            "BinaryRequiresExplicitProvider",
        )

    @override_settings(YTDLP_BINARY="")
    def test_inspect_third_party_provider_binary_rejects_kukutool(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i",
            provider="kukutool",
            binary="/usr/local/bin/node",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["source_url"],
            "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i",
        )
        self.assertEqual(payload["platform_id"], "kuaishou")
        self.assertEqual(payload["attempts"], [])
        self.assertEqual(payload["errors"][0]["provider"], "kukutool")
        self.assertEqual(payload["errors"][0]["status"], "rejected")
        self.assertEqual(payload["errors"][0]["error_type"], "UnsupportedBinaryProvider")
        self.assertEqual(settings.YTDLP_BINARY, "")

    def test_inspect_third_party_provider_reports_disallowed_provider_json(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.youtube.com/watch?v=demo",
            provider="gallerydl",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["source_url"], "https://www.youtube.com/watch?v=demo")
        self.assertEqual(payload["platform_id"], "youtube")
        self.assertEqual(payload["attempts"], [])
        self.assertEqual(payload["errors"][0]["provider"], "gallerydl")
        self.assertEqual(payload["errors"][0]["status"], "rejected")
        self.assertEqual(payload["errors"][0]["error_type"], "ProviderNotAllowed")
        self.assertIn("YouTube", payload["errors"][0]["error"])

    @override_settings(
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_inspect_third_party_provider_reports_no_enabled_provider_json(self):
        output = StringIO()

        call_command(
            "inspect_third_party_provider",
            "https://www.youtube.com/watch?v=demo",
            provider="auto",
            as_json=True,
            stdout=output,
        )
        payload = json.loads(output.getvalue())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["source_url"], "https://www.youtube.com/watch?v=demo")
        self.assertEqual(payload["platform_id"], "youtube")
        self.assertEqual(payload["attempts"], [])
        self.assertEqual(payload["errors"][0]["provider"], "auto")
        self.assertEqual(payload["errors"][0]["status"], "rejected")
        self.assertEqual(payload["errors"][0]["error_type"], "NoEnabledProvider")
        self.assertIn("No enabled provider", payload["errors"][0]["error"])
        self.assertIn("kukutool_generic", payload["errors"][0]["error"])
        self.assertIn("yt_dlp_generic", payload["errors"][0]["error"])

    def test_kukutool_normalizer_returns_standard_video_result(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i"
        )
        payload = {
            "title": "kuaishou post",
            "cover": "https://p1.a.yximgs.com/cover.jpg",
            "videos": ["https://hwmov.a.kwimgs.com/demo.mp4"],
            "pics": ["https://p1.a.yximgs.com/cover.jpg"],
        }

        result = KukutoolResultNormalizer().normalize(
            rule=rule,
            platform_name="快手",
            source_url="https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i",
            payload=payload,
        )

        self.assertEqual(result.platform, "third_party")
        self.assertEqual(result.post_type, "video")
        self.assertEqual(result.metadata["source_provider"], "kukutool_generic")
        self.assertEqual(result.video_assets[0].url, payload["videos"][0])
        self.assertEqual(result.video_assets[0].cover_url, payload["cover"])
        self.assertTrue(result.video_assets[0].provider_manifest_token)

    def test_kukutool_normalizer_returns_standard_image_result(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.instagram.com/p/example/"
        )
        payload = {
            "title": "image post",
            "pics": [
                "https://scontent.cdninstagram.com/image-1.jpg",
                "https://scontent.cdninstagram.com/image-2.jpg",
            ],
        }

        result = KukutoolResultNormalizer().normalize(
            rule=rule,
            platform_name="Instagram",
            source_url="https://www.instagram.com/p/example/",
            payload=payload,
        )

        self.assertEqual(result.post_type, "image")
        self.assertEqual(len(result.image_assets), 2)
        self.assertEqual(result.image_assets[0].provider, "kukutool_generic")
        self.assertTrue(result.image_assets[0].provider_manifest_token)

    def test_kukutool_normalizer_rejects_unlisted_media_host(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.instagram.com/p/example/"
        )

        with self.assertLogs(
            "xhs_download.services.platforms.kukutool_generic",
            level="WARNING",
        ) as logs:
            with self.assertRaisesRegex(ValueError, "media assets"):
                KukutoolResultNormalizer().normalize(
                    rule=rule,
                    platform_name="Instagram",
                    source_url="https://www.instagram.com/p/example/",
                    payload={"videos": ["https://evil.example.com/video.mp4"]},
                )

        self.assertIn("third_party_provider_url_rejected", "\n".join(logs.output))

    def test_kukutool_normalizer_logs_rejected_host_without_query(self):
        rule = GenericThirdPartyAdapter().get_rule_for_url(
            "https://www.instagram.com/p/example/"
        )

        with self.assertLogs(
            "xhs_download.services.platforms.kukutool_generic",
            level="WARNING",
        ) as logs:
            with self.assertRaisesRegex(ValueError, "media assets"):
                KukutoolResultNormalizer().normalize(
                    rule=rule,
                    platform_name="Instagram",
                    source_url="https://www.instagram.com/p/example/",
                    payload={
                        "videos": [
                            "https://evil.example.com/video.mp4?token=secret"
                        ]
                    },
                )

        output = "\n".join(logs.output)
        self.assertIn("third_party_provider_url_rejected", output)
        self.assertIn("platform=instagram", output)
        self.assertIn("host=evil.example.com", output)
        self.assertIn("reason=host_not_allowed", output)
        self.assertNotIn("token=secret", output)

    def test_third_party_source_key_ignores_common_tracking_query(self):
        first = source_key_for_url(
            "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i"
            "?authorId=1&streamSource=find&area=home&utm_source=share"
        )
        second = source_key_for_url(
            "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i"
            "?authorId=2&streamSource=profile&area=detail&utm_source=copy"
        )

        self.assertEqual(first, second)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_generic_third_party_parse_accepts_string_video_entries(self):
        media_url = "https://hwmov.a.kwimgs.com/demo.mp4"
        runner = Mock(return_value={
            "title": "kuaishou post",
            "cover": "https://p1.a.yximgs.com/cover.jpg",
            "videos": [media_url],
            "pics": ["https://p1.a.yximgs.com/cover.jpg"],
        })

        result = GenericThirdPartyAdapter(runner=runner).parse_url(
            "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i"
        )

        self.assertEqual(result.post_type, "video")
        self.assertEqual(result.metadata["source_platform"], "kuaishou")
        self.assertEqual(result.video_assets[0].url, media_url)
        self.assertEqual(result.video_assets[0].quality_label, "video")
        self.assertEqual(result.video_assets[0].provider, "kukutool_generic")
        self.assertTrue(result.video_assets[0].provider_manifest_token)

    @override_settings(THIRD_PARTY_KUKUTOOL_RETRIES=2)
    def test_kukutool_generic_provider_retries_transient_failure(self):
        runner = Mock(side_effect=[
            OSError("parse failed (500): retry"),
            {
                "title": "retry post",
                "videos": ["https://scontent.cdninstagram.com/video.mp4"],
            },
        ])

        payload = KukutoolGenericProvider(runner=runner).parse(
            "https://www.instagram.com/p/example/"
        )

        self.assertEqual(payload["title"], "retry post")
        self.assertEqual(runner.call_count, 2)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_generic_third_party_adapter_delegates_to_provider_and_normalizer(self):
        provider = Mock()
        provider.parse.return_value = {
            "title": "delegated post",
            "videos": ["https://scontent.cdninstagram.com/video.mp4"],
        }
        normalizer = KukutoolResultNormalizer()
        adapter = GenericThirdPartyAdapter(provider=provider, normalizer=normalizer)

        result = adapter.parse_url("https://www.instagram.com/p/example/")

        self.assertEqual(result.post_type, "video")
        provider.parse.assert_called_once_with(
            "https://www.instagram.com/p/example/",
            timeout_seconds=ANY,
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_generic_third_party_adapter_extracts_url_from_share_text(self):
        provider = Mock()
        provider.parse.return_value = {
            "title": "shared post",
            "videos": ["https://scontent.cdninstagram.com/video.mp4"],
        }
        adapter = GenericThirdPartyAdapter(
            provider=provider,
            normalizer=KukutoolResultNormalizer(),
        )

        result = adapter.parse_url(
            "Check this https://www.instagram.com/p/example/ ，nice"
        )

        self.assertEqual(result.post_type, "video")
        self.assertEqual(
            result.metadata["canonical_source_url"],
            "https://www.instagram.com/p/example/",
        )
        provider.parse.assert_called_once_with(
            "https://www.instagram.com/p/example/",
            timeout_seconds=ANY,
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
        THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=2.5,
    )
    def test_generic_third_party_adapter_passes_remaining_budget_to_fallback(self):
        timeouts = []

        def fail_parse(_url, timeout_seconds=None):
            timeouts.append(timeout_seconds)
            raise RuntimeError("first provider failed")

        def ytdlp_parse(_url, timeout_seconds=None):
            timeouts.append(timeout_seconds)
            return {
                "title": "budgeted fallback",
                "formats": [
                    {
                        "url": "https://v16.tiktokcdn.com/video/demo.mp4",
                        "protocol": "https",
                        "ext": "mp4",
                        "vcodec": "avc1.4d401f",
                        "acodec": "mp4a.40.2",
                        "height": 720,
                        "filesize": 300,
                        "format_note": "720p",
                    }
                ],
            }

        adapter = GenericThirdPartyAdapter(
            provider=Mock(parse=fail_parse),
            ytdlp_provider=Mock(parse=ytdlp_parse),
        )

        result = adapter.parse_url("https://www.tiktok.com/@demo/video/123")

        self.assertEqual(result.metadata["selected_provider"], "yt_dlp_generic")
        self.assertEqual(len(timeouts), 2)
        self.assertGreater(timeouts[0], 0)
        self.assertGreater(timeouts[1], 0)
        self.assertLessEqual(timeouts[0], 2.5)
        self.assertLessEqual(timeouts[1], timeouts[0])
