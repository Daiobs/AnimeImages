from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import Resolver404, resolve, reverse

from tools.provider_lab.runtime.workers import parsehub_worker, videodl_worker
from xhs_download.services.assets import MediaAsset, ParseResult
from xhs_download.services.provider_lab import contracts
from xhs_download.services.provider_lab.contracts import (
    LabAssetV1,
    LabProviderResultV1,
    ProviderLabError,
    sanitize_raw_shape,
    validated_resource_url,
)
from xhs_download.services.provider_lab.providers import (
    PARSEHUB_PIN,
    PROVIDER_REGISTRY,
    VIDEODL_PIN,
    _worker_result_to_contract,
    list_provider_descriptors,
    normalized_source,
    run_animeimages_current,
    run_tiantian_existing,
    run_ytdlp_existing,
)
from xhs_download.services.provider_lab.resources import (
    RESOURCE_MAX_USES,
    consume_resource_token,
    issue_resource_token,
    public_resource_descriptor,
    resource_cache_key,
)
from xhs_download.services.provider_lab.runtime import (
    clean_worker_environment,
    environment_is_clean,
    execute_provider_worker,
    parse_worker_stdout,
)


BASE_DIR = Path(__file__).resolve().parents[1]
PARSEHUB_WORKER = BASE_DIR / "tools/provider_lab/runtime/workers/parsehub_worker.py"
VIDEODL_WORKER = BASE_DIR / "tools/provider_lab/runtime/workers/videodl_worker.py"


@override_settings(
    UPSTREAM_PROVIDER_LAB_ENABLED=True,
    VIDEODL_PROVIDER_LAB_ENABLED=False,
    TIANTIANJIEXI_PROVIDER_ENABLED=False,
    YTDLP_PROVIDER_ENABLED=False,
)
class ProviderLabAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        user_model = get_user_model()
        self.staff = user_model.objects.create_user(
            username="provider-lab-staff",
            password="test-password",
            is_staff=True,
            is_active=True,
        )
        self.non_staff = user_model.objects.create_user(
            username="provider-lab-user",
            password="test-password",
            is_staff=False,
            is_active=True,
        )
        self.inactive_staff = user_model.objects.create_user(
            username="provider-lab-inactive",
            password="test-password",
            is_staff=True,
            is_active=False,
        )

    @override_settings(UPSTREAM_PROVIDER_LAB_ENABLED=False)
    def test_01_feature_flag_off_returns_404(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("provider_lab")).status_code, 404)

    def test_02_anonymous_returns_404(self):
        self.assertEqual(self.client.get(reverse("provider_lab")).status_code, 404)

    def test_03_non_staff_returns_404(self):
        self.client.force_login(self.non_staff)
        self.assertEqual(self.client.get(reverse("provider_lab")).status_code, 404)

    def test_04_inactive_staff_returns_404(self):
        self.client.force_login(self.inactive_staff)
        self.assertEqual(self.client.get(reverse("provider_lab")).status_code, 404)

    def test_05_active_staff_is_allowed(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("provider_lab"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "上游 Provider 测试工作台")

    def test_06_page_is_no_store_and_nosniff(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("provider_lab"))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_07_run_endpoint_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        response = client.post(
            reverse("provider_lab_run"),
            data=json.dumps(
                {"source": "https://example.com/post", "provider_id": "animeimages_current"}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_08_unknown_provider_is_rejected(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("provider_lab_run"),
            data=json.dumps({"source": "https://example.com/post", "provider_id": "unknown"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "PROVIDER_NOT_SELECTED")

    def test_09_run_schema_rejects_extra_fields(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("provider_lab_run"),
            data=json.dumps(
                {
                    "source": "https://example.com/post",
                    "provider_id": "animeimages_current",
                    "provider_order": ["other"],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "PROVIDER_SCHEMA_INVALID")

    @mock.patch("xhs_download.provider_lab_views.run_provider")
    def test_10_run_executes_exactly_one_provider(self, run_provider_mock):
        run_provider_mock.return_value = {
            "schema_version": "1.0",
            "provider_id": "animeimages_current",
            "status": "success",
            "assets": [],
        }
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("provider_lab_run"),
            data=json.dumps(
                {"source": "https://example.com/post", "provider_id": "animeimages_current"}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        run_provider_mock.assert_called_once()
        self.assertEqual(run_provider_mock.call_args.kwargs["provider_id"], "animeimages_current")

    @mock.patch("xhs_download.provider_lab_views.run_provider")
    def test_11_completed_execution_can_be_retried(self, run_provider_mock):
        run_provider_mock.return_value = {"schema_version": "1.0", "status": "success"}
        self.client.force_login(self.staff)
        body = json.dumps(
            {"source": "https://example.com/same", "provider_id": "animeimages_current"}
        )
        first = self.client.post(reverse("provider_lab_run"), body, content_type="application/json")
        second = self.client.post(reverse("provider_lab_run"), body, content_type="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(run_provider_mock.call_count, 2)

    @mock.patch("xhs_download.provider_lab_views.run_provider")
    def test_11b_inflight_execution_is_single_flight_limited(self, run_provider_mock):
        self.client.force_login(self.staff)
        body = json.dumps({"source": "https://example.com/same", "provider_id": "animeimages_current"})
        def during_execution(**kwargs):
            duplicate = self.client.post(reverse("provider_lab_run"), body, content_type="application/json")
            self.assertEqual(duplicate.status_code, 429)
            self.assertEqual(duplicate.json()["error_code"], "PROVIDER_RATE_LIMITED")
            return {"schema_version": "1.0", "status": "success"}
        run_provider_mock.side_effect = during_execution
        response = self.client.post(reverse("provider_lab_run"), body, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        run_provider_mock.assert_called_once()

    @mock.patch("xhs_download.provider_lab_views.consume_fixed_window")
    def test_12_execution_rate_limit_is_enforced(self, limit_mock):
        limit_mock.return_value = SimpleNamespace(allowed=False)
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("provider_lab_run"),
            json.dumps(
                {"source": "https://example.com/rate", "provider_id": "animeimages_current"}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 429)

    def test_13_page_contains_noncommercial_license_warning(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("provider_lab"))
        self.assertContains(response, "PolyForm Noncommercial 1.0.0")
        self.assertContains(response, "仅限非商业本地研究测试")

    def test_14_initial_html_contains_no_media_url(self):
        self.client.force_login(self.staff)
        content = self.client.get(reverse("provider_lab")).content.decode()
        self.assertNotIn("365yg.com", content)
        self.assertNotIn("xhscdn.com", content)
        self.assertNotIn("googlevideo.com", content)

    def test_15_no_public_provider_lab_route_exists(self):
        with self.assertRaises(Resolver404):
            resolve("/provider-lab/")


class ProviderLabRegistryAndContractTests(SimpleTestCase):
    def test_16_registry_is_explicit_and_complete(self):
        self.assertEqual(
            tuple(PROVIDER_REGISTRY),
            (
                "animeimages_current",
                "parsehub_repo",
                "videodl_repo",
                "yt_dlp_existing",
                "gallery_dl_existing",
                "tiantian_existing",
            ),
        )

    def test_17_registry_contains_no_dynamic_import(self):
        source = (BASE_DIR / "xhs_download/services/provider_lab/providers.py").read_text()
        self.assertNotIn("import_module", source)
        self.assertNotIn("entry_points", source)

    def test_18_all_six_providers_can_be_compared(self):
        self.assertEqual(len(PROVIDER_REGISTRY), 6)

    def test_19_share_text_extracts_one_url(self):
        self.assertEqual(
            normalized_source("看看这个 https://example.com/post?id=1 结束"),
            "https://example.com/post?id=1",
        )

    def test_20_overlong_source_is_rejected(self):
        with self.assertRaises(ProviderLabError):
            normalized_source("https://example.com/" + "x" * 4096)

    def test_21_raw_shape_redacts_urls(self):
        result = sanitize_raw_shape({"media": "https://cdn.example/video.mp4"})
        self.assertEqual(result["media"], "[redacted]")

    def test_21a_raw_shape_redacts_protocol_relative_and_bare_urls(self):
        result = sanitize_raw_shape(
            {
                "first": "//cdn.example/media.mp4",
                "second": "cdn.example/media.mp4?token=hidden",
            }
        )
        self.assertEqual(result["first"], "[redacted]")
        self.assertEqual(result["second"], "[redacted]")

    def test_21b_raw_shape_redacts_ipv4_and_ipv6_text(self):
        result = sanitize_raw_shape(
            {
                "first": "upstream 192.0.2.10 unavailable",
                "second": "upstream [2001:db8::1]:443 unavailable",
            }
        )
        self.assertEqual(result["first"], "[redacted]")
        self.assertEqual(result["second"], "[redacted]")

    def test_21c_worker_raw_shape_uses_the_same_url_boundary(self):
        result = parsehub_worker.raw_shape(
            {"value": "cdn.example/private?token=hidden"}
        )
        self.assertEqual(result["value"], "[redacted]")

    def test_22_raw_shape_redacts_cookie_and_header_values(self):
        result = sanitize_raw_shape(
            {"cookie": "session=secret", "headers": {"Referer": "secret"}}
        )
        self.assertEqual(result["cookie"], "[redacted]")
        self.assertEqual(result["headers"], "[redacted]")

    def test_23_raw_shape_redacts_local_paths(self):
        result = sanitize_raw_shape({"working_directory": "/Users/private/provider"})
        self.assertEqual(result["working_directory"], "[redacted]")

    def test_24_raw_shape_limits_depth(self):
        value = current = {}
        for _index in range(10):
            current["child"] = {}
            current = current["child"]
        encoded = json.dumps(sanitize_raw_shape(value))
        self.assertIn("max_depth", encoded)

    def test_25_raw_shape_limits_mapping_keys(self):
        result = sanitize_raw_shape({f"key_{index}": index for index in range(150)})
        self.assertEqual(result["_truncated_keys"], 22)

    def test_26_raw_shape_limits_list_inspection(self):
        result = sanitize_raw_shape({"items": list(range(80))})
        self.assertEqual(result["items"]["count"], 80)
        self.assertEqual(len(result["items"]["items"]), 64)

    def test_27_public_metadata_redacts_urls(self):
        result = LabProviderResultV1(
            request_id="request123",
            provider_id="test",
            provider_version="test@1",
            provider_scope="lab_only",
            status="success",
            metadata={"title": "https://private.example/media"},
        ).as_dict()
        self.assertEqual(result["metadata"]["title"], "[redacted]")

    def test_28_asset_contract_contains_no_url_field(self):
        asset_data = LabAssetV1(asset_id="asset-1", host_class="cdn.example").as_dict()
        self.assertNotIn("url", asset_data)

    def test_29_private_literal_resource_url_is_rejected(self):
        with self.assertRaises(ProviderLabError):
            validated_resource_url("http://127.0.0.1/private")

    def test_30_credentialed_resource_url_is_rejected(self):
        with self.assertRaises(ProviderLabError):
            validated_resource_url("https://user:pass@example.com/media")

    @override_settings(
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_BINARY=sys.executable,
        TIANTIANJIEXI_PROVIDER_ENABLED=False,
    )
    @mock.patch(
        "xhs_download.services.provider_lab.providers.TianTianJieXiGenericProvider.health"
    )
    @mock.patch(
        "xhs_download.services.provider_lab.providers.YtDlpGenericProvider.health"
    )
    def test_30a_descriptor_listing_does_not_probe_upstreams(
        self,
        ytdlp_health_mock,
        tiantian_health_mock,
    ):
        descriptors = list_provider_descriptors()
        self.assertEqual(len(descriptors), 6)
        ytdlp_health_mock.assert_not_called()
        tiantian_health_mock.assert_not_called()


class ProviderLabWorkerProtocolTests(SimpleTestCase):
    def _script(self, source):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "worker.py"
        path.write_text(source, encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return path

    def _valid_script(self, raw_expression="{}"):
        return self._script(
            "import json, os, sys\n"
            "request=json.load(sys.stdin)\n"
            f"raw={raw_expression}\n"
            "payload={'schema_version':'1.0','result':{'request_id':request['request_id']},"
            "'resources':[],'raw':raw}\n"
            "json.dump(payload,sys.stdout)\n"
        )

    def test_31_parsehub_exact_pin_is_present(self):
        self.assertEqual(PARSEHUB_PIN, "144260f4349c5bb57415f7555a572fcd48c3b8ba")
        self.assertIn(PARSEHUB_PIN, PARSEHUB_WORKER.read_text())

    def test_32_videodl_exact_pin_is_present(self):
        self.assertEqual(VIDEODL_PIN, "6cd1de0fe1209380b183b654c65e98a629637873")
        self.assertIn(VIDEODL_PIN, VIDEODL_WORKER.read_text())

    def test_33_clean_environment_uses_temporary_home(self):
        env = clean_worker_environment(home="/tmp/lab-home", config_home="/tmp/cfg", cache_home="/tmp/cache")
        self.assertEqual(env["HOME"], "/tmp/lab-home")
        self.assertEqual(env["XDG_CONFIG_HOME"], "/tmp/cfg")

    @mock.patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.invalid"}, clear=False)
    def test_34_proxy_environment_is_not_inherited(self):
        env = clean_worker_environment(home="/tmp/home", config_home="/tmp/cfg", cache_home="/tmp/cache")
        self.assertNotIn("HTTP_PROXY", env)
        self.assertTrue(environment_is_clean(env))

    @mock.patch.dict(os.environ, {"PROVIDER_COOKIE": "secret"}, clear=False)
    def test_35_cookie_environment_is_not_inherited(self):
        env = clean_worker_environment(home="/tmp/home", config_home="/tmp/cfg", cache_home="/tmp/cache")
        self.assertNotIn("PROVIDER_COOKIE", env)

    def test_36_invalid_worker_json_is_rejected(self):
        with self.assertRaises(ProviderLabError) as context:
            parse_worker_stdout("not-json")
        self.assertEqual(context.exception.code, "PROVIDER_INVALID_JSON")

    def test_37_invalid_worker_schema_is_rejected(self):
        with self.assertRaises(ProviderLabError) as context:
            parse_worker_stdout(json.dumps({"schema_version": "2.0"}))
        self.assertEqual(context.exception.code, "PROVIDER_SCHEMA_INVALID")

    def test_38_worker_timeout_is_bounded(self):
        script = self._script("import time\ntime.sleep(5)\n")
        started = time.monotonic()
        with self.assertRaises(ProviderLabError) as context:
            execute_provider_worker(
                python=sys.executable,
                worker=script,
                request_id="request_timeout",
                source="https://example.com/post",
                timeout_seconds=0.2,
            )
        self.assertEqual(context.exception.code, "PROVIDER_TIMEOUT")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_39_worker_stdout_overflow_terminates(self):
        script = self._script(
            "import sys,time\nsys.stdout.write('x'*(1024*1024+4096));sys.stdout.flush();time.sleep(2)\n"
        )
        with self.assertRaises(ProviderLabError) as context:
            execute_provider_worker(
                python=sys.executable,
                worker=script,
                request_id="request_stdout",
                source="https://example.com/post",
                timeout_seconds=2,
            )
        self.assertEqual(context.exception.code, "PROVIDER_OUTPUT_TOO_LARGE")

    def test_40_worker_stderr_overflow_is_bounded(self):
        script = self._script(
            "import sys,time\nsys.stderr.write('x'*(128*1024+4096));sys.stderr.flush();time.sleep(2)\n"
        )
        with self.assertRaises(ProviderLabError) as context:
            execute_provider_worker(
                python=sys.executable,
                worker=script,
                request_id="request_stderr",
                source="https://example.com/post",
                timeout_seconds=2,
            )
        self.assertEqual(context.exception.code, "PROVIDER_OUTPUT_TOO_LARGE")

    def test_41_temporary_directory_is_cleaned(self):
        script = self._valid_script("{'home':os.environ['HOME'],'cwd':os.getcwd()}")
        payload = execute_provider_worker(
            python=sys.executable,
            worker=script,
            request_id="request_cleanup",
            source="https://example.com/post",
            timeout_seconds=2,
        )
        self.assertFalse(Path(payload["raw"]["home"]).exists())
        self.assertFalse(Path(payload["raw"]["cwd"]).exists())

    def test_42_timeout_cleans_child_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            script = self._script(
                "import subprocess,time\n"
                f"p=subprocess.Popen([{sys.executable!r},'-c','import time;time.sleep(10)'])\n"
                f"open({str(pid_file)!r},'w').write(str(p.pid))\n"
                "time.sleep(10)\n"
            )
            with self.assertRaises(ProviderLabError):
                execute_provider_worker(
                    python=sys.executable,
                    worker=script,
                    request_id="request_children",
                    source="https://example.com/post",
                    timeout_seconds=0.3,
                )
            child_pid = int(pid_file.read_text())
            deadline = time.monotonic() + 1.5
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(alive)

    @mock.patch("xhs_download.services.provider_lab.runtime.parse_worker_stdout")
    @mock.patch("xhs_download.services.provider_lab.runtime._execute_external_process")
    def test_42b_virtualenv_python_symlink_is_not_resolved(
        self,
        execute_process,
        parse_stdout,
    ):
        execute_process.return_value = SimpleNamespace(returncode=0)
        parse_stdout.return_value = {
            "schema_version": "1.0",
            "result": {},
            "resources": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            python_link = Path(directory) / "venv-python"
            python_link.symlink_to(Path(sys.executable))
            resolved_python = python_link.resolve()
            script = self._script("pass\n")
            execute_provider_worker(
                python=str(python_link),
                worker=script,
                request_id="request_venv",
                source="https://example.com/post",
                timeout_seconds=2,
            )
        command = execute_process.call_args.args[0]
        self.assertEqual(command[0], str(python_link.absolute()))
        self.assertNotEqual(command[0], str(resolved_python))

    def test_43_parsehub_worker_never_calls_download(self):
        tree = ast.parse(PARSEHUB_WORKER.read_text())
        called = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertNotIn("download", called)
        self.assertNotIn("download_sync", called)

    def test_44_videodl_worker_never_calls_download(self):
        tree = ast.parse(VIDEODL_WORKER.read_text())
        called = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertNotIn("download", called)


class ProviderLabNormalizerTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_45_parsehub_image_result_is_normalized(self):
        class ImageRef:
            def __init__(self):
                self.url = "https://img.example.com/a.jpg"
                self.ext = "jpg"
                self.width = 800
                self.height = 600

        ImageParseResult = type("ImageParseResult", (), {})
        result = ImageParseResult()
        result.media = [ImageRef()]
        result.title = "Image title"
        result.content = "summary"
        result.platform = SimpleNamespace(id="xhs")
        public, resources = parsehub_worker.normalize_result(
            result,
            {"request_id": "request_image", "max_assets": 64},
        )
        self.assertEqual(public["result_kind"], "image")
        self.assertEqual(public["assets"][0]["media_type"], "image")
        self.assertEqual(resources[0]["url"], "https://img.example.com/a.jpg")

    def test_46_parsehub_video_result_is_normalized(self):
        VideoRef = type("VideoRef", (), {})
        item = VideoRef()
        item.url = "https://video.example.com/a.mp4"
        item.ext = "mp4"
        item.width = 1920
        item.height = 1080
        item.duration = 12
        Result = type("VideoParseResult", (), {})
        result = Result()
        result.media = item
        result.title = "Video title"
        result.content = ""
        result.platform = SimpleNamespace(id="bilibili")
        public, _resources = parsehub_worker.normalize_result(
            result,
            {"request_id": "request_video", "max_assets": 64},
        )
        self.assertEqual(public["result_kind"], "video")
        self.assertEqual(public["assets"][0]["duration_seconds"], 12.0)

    def test_47_parsehub_livephoto_relationship_is_item_local(self):
        LivePhotoRef = type("LivePhotoRef", (), {})
        item = LivePhotoRef()
        item.url = "https://img.example.com/still.jpg"
        item.video_url = "https://video.example.com/motion.mp4"
        item.ext = "jpg"
        item.video_ext = "mp4"
        item.width = 1000
        item.height = 1500
        item.duration = 3
        Result = type("ImageParseResult", (), {})
        result = Result()
        result.media = [item]
        result.title = "Live"
        result.content = ""
        result.platform = SimpleNamespace(id="xhs")
        public, _resources = parsehub_worker.normalize_result(
            result,
            {"request_id": "request_live", "max_assets": 64},
        )
        self.assertEqual(public["result_kind"], "livephoto")
        self.assertEqual(len(public["relationships"]), 1)
        self.assertEqual(public["relationships"][0]["relationship_type"], "livephoto_motion")

    def test_48_parsehub_unknown_result_fails_closed_without_media(self):
        result = SimpleNamespace(media=None, title="", content="", platform=None)
        with self.assertRaises(parsehub_worker.WorkerError) as context:
            parsehub_worker.normalize_result(
                result,
                {"request_id": "request_unknown", "max_assets": 64},
            )
        self.assertEqual(context.exception.code, "PROVIDER_RESULT_EMPTY")

    def _video_info(self, **overrides):
        defaults = {
            "source": "BilibiliVideoClient",
            "title": "Video title",
            "cover_url": "https://img.example.com/cover.jpg",
            "download_url": "https://video.example.com/video.mp4",
            "default_download_headers": {"Referer": "https://bilibili.com"},
            "default_download_cookies": None,
            "audio_download_url": "",
            "default_audio_download_headers": None,
            "default_audio_download_cookies": None,
            "ext": "mp4",
            "audio_ext": "m4a",
            "download_with_ffmpeg": False,
            "identifier": "private-id",
            "err_msg": "",
            "raw_data": {"signed_url": "https://secret.example/video"},
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_49_videodl_single_video_info_is_normalized(self):
        public, resources = videodl_worker.normalize_result(
            [self._video_info()],
            {"request_id": "request_videodl", "max_assets": 64},
        )
        self.assertEqual(public["result_kind"], "video")
        self.assertGreaterEqual(len(resources), 1)

    def test_50_videodl_multiple_video_infos_are_preserved(self):
        public, _resources = videodl_worker.normalize_result(
            [
                self._video_info(download_url="https://video.example.com/one.mp4"),
                self._video_info(download_url="https://video.example.com/two.mp4"),
            ],
            {"request_id": "request_multi", "max_assets": 64},
        )
        primary = [item for item in public["assets"] if item["media_type"] == "video"]
        self.assertEqual(len(primary), 2)

    def test_51_videodl_audio_url_becomes_audio_asset(self):
        public, _resources = videodl_worker.normalize_result(
            [self._video_info(audio_download_url="https://audio.example.com/a.m4a")],
            {"request_id": "request_audio", "max_assets": 64},
        )
        self.assertIn("audio", {item["media_type"] for item in public["assets"]})

    def test_52_videodl_separate_audio_requires_merge(self):
        public, _resources = videodl_worker.normalize_result(
            [self._video_info(audio_download_url="https://audio.example.com/a.m4a")],
            {"request_id": "request_merge", "max_assets": 64},
        )
        video = next(item for item in public["assets"] if item["media_type"] == "video")
        self.assertTrue(video["requires_merge"])

    def test_53_videodl_cookie_values_are_redacted(self):
        public, _resources = videodl_worker.normalize_result(
            [self._video_info(default_download_cookies={"session": "secret"})],
            {"request_id": "request_cookie", "max_assets": 64},
        )
        encoded = json.dumps(public["raw_shape"])
        self.assertNotIn("secret", encoded)
        self.assertIn("redacted", encoded)

    def test_54_videodl_header_values_are_redacted(self):
        public, _resources = videodl_worker.normalize_result(
            [self._video_info(default_download_headers={"Referer": "private-value"})],
            {"request_id": "request_header", "max_assets": 64},
        )
        encoded = json.dumps(public["raw_shape"])
        self.assertNotIn("private-value", encoded)

    @mock.patch("xhs_download.services.provider_lab.providers.parse_media_page")
    def test_55_current_provider_reuses_parse_media_page(self, parse_mock):
        parse_mock.return_value = ParseResult(
            platform="xhs",
            post_type="image",
            base_filename="Current",
            assets=(
                MediaAsset(
                    media_type="image",
                    url="https://img.example.com/current.jpg",
                    platform="xhs",
                    ext="jpg",
                ),
            ),
        )
        result, resources = run_animeimages_current(
            source="https://example.com/post",
            request_id="request_current",
            user_id=1,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(resources, [])
        parse_mock.assert_called_once()

    def test_56_worker_result_issues_opaque_tokens(self):
        descriptor = PROVIDER_REGISTRY["parsehub_repo"]
        result_data = {
            "request_id": "request_worker",
            "provider_id": "parsehub_repo",
            "provider_version": descriptor.provider_version,
            "status": "success",
            "claimed_platform": "xhs",
            "selected_backend": "xhs",
            "result_kind": "image",
            "metadata": {},
            "assets": [
                {
                    "asset_id": "asset-one",
                    "media_type": "image",
                    "role": "primary",
                    "host_class": "img.example.com",
                }
            ],
            "relationships": [],
            "warnings": [],
            "raw_shape": {},
        }
        contract = _worker_result_to_contract(
            result_data,
            [{"asset_id": "asset-one", "url": "https://img.example.com/a.jpg"}],
            descriptor=descriptor,
            user_id=7,
            request_id="request_worker",
            elapsed_ms=12,
        ).as_dict()
        self.assertTrue(contract["assets"][0]["resource_token"])
        self.assertNotIn("https://", json.dumps(contract))

    def test_56a_ytdlp_lab_path_is_metadata_only(self):
        source = (
            BASE_DIR / "xhs_download/services/platforms/ytdlp_generic.py"
        ).read_text()
        self.assertIn('"--skip-download"', source)
        lab_source = (
            BASE_DIR / "xhs_download/services/provider_lab/providers.py"
        ).read_text()
        self.assertNotIn(".download(", lab_source)
        self.assertIn("prefer_youtube_merge=False", lab_source)

    @mock.patch(
        "xhs_download.services.quality_execution.execute_provider"
    )
    def test_56b_ytdlp_wrapper_issues_only_lab_tokens(self, parse_mock):
        parse_mock.return_value = ParseResult(platform="third_party", post_type="video",
            base_filename="Video", assets=(MediaAsset(platform="third_party", url="https://video.example.com/video.mp4", media_type="video"),),
            metadata={"title": "Video"})
        result, resources = run_ytdlp_existing(
            source="https://example.com/post",
            request_id="request_ytdlp",
            user_id=1,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(resources, [])
        self.assertNotIn("provider_manifest", json.dumps(result))

    @mock.patch(
        "xhs_download.services.provider_lab.providers.TianTianJieXiGenericProvider.health"
    )
    @mock.patch(
        "xhs_download.services.provider_lab.providers.TianTianJieXiGenericProvider.parse"
    )
    @mock.patch(
        "xhs_download.services.provider_lab.providers.tiantian_provider_configured",
        return_value=True,
    )
    def test_56c_tiantian_wrapper_does_not_sign_production_manifest(
        self,
        _configured_mock,
        parse_mock,
        health_mock,
    ):
        parse_mock.return_value = {
            "title": "Video",
            "videos": [
                {
                    "url": "https://video.example.com/video.mp4",
                    "quality": "source",
                }
            ],
        }
        result, resources = run_tiantian_existing(
            source="https://example.com/post",
            request_id="request_tiantian",
            user_id=1,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(resources, [])
        self.assertNotIn("provider_manifest", json.dumps(result))
        health_mock.assert_not_called()


class ProviderLabResourceTokenTests(TestCase):
    def setUp(self):
        cache.clear()
        self.values = {
            "user_id": 101,
            "request_id": "request_resource",
            "provider_id": "parsehub_repo",
            "asset_id": "asset-resource",
            "url": "https://img.example.com/private.jpg?signature=hidden",
            "media_type": "image",
        }

    def _token(self, **overrides):
        values = {**self.values, **overrides}
        return issue_resource_token(**values)

    def test_57_valid_staff_bound_token_returns_descriptor(self):
        token = self._token()
        descriptor = consume_resource_token(token=token, **{key: self.values[key] for key in ("user_id", "request_id", "provider_id", "asset_id")})
        self.assertEqual(descriptor.url, self.values["url"])

    def test_58_wrong_user_is_rejected(self):
        token = self._token()
        with self.assertRaises(ProviderLabError):
            consume_resource_token(
                token=token,
                user_id=999,
                request_id=self.values["request_id"],
                provider_id=self.values["provider_id"],
                asset_id=self.values["asset_id"],
            )

    def test_59_expired_token_is_rejected(self):
        token = self._token()
        cache.delete(resource_cache_key(token))
        with self.assertRaises(ProviderLabError):
            consume_resource_token(
                token=token,
                **{key: self.values[key] for key in ("user_id", "request_id", "provider_id", "asset_id")},
            )

    def test_59a_token_uses_fixed_issuance_expiry(self):
        token = self._token()
        payload = cache.get(resource_cache_key(token))
        payload["expires_at"] = time.time() - 1
        cache.set(resource_cache_key(token), payload, timeout=300)
        with self.assertRaises(ProviderLabError):
            consume_resource_token(
                token=token,
                **{
                    key: self.values[key]
                    for key in ("user_id", "request_id", "provider_id", "asset_id")
                },
            )

    def test_60_altered_asset_is_rejected(self):
        token = self._token()
        with self.assertRaises(ProviderLabError):
            consume_resource_token(
                token=token,
                user_id=self.values["user_id"],
                request_id=self.values["request_id"],
                provider_id=self.values["provider_id"],
                asset_id="altered",
            )

    def test_61_token_itself_contains_no_url(self):
        token = self._token()
        self.assertNotIn("example.com", token)
        self.assertNotIn("signature", token)

    def test_62_descriptor_endpoint_contract_contains_no_media_body(self):
        token = self._token()
        descriptor = consume_resource_token(token=token, **{key: self.values[key] for key in ("user_id", "request_id", "provider_id", "asset_id")})
        payload = public_resource_descriptor(descriptor)
        self.assertEqual(payload["max_probe_bytes"], 4096)
        self.assertNotIn("body", payload)

    def test_63_header_bound_resource_does_not_expose_url(self):
        token = self._token(headers_present=True)
        descriptor = consume_resource_token(token=token, **{key: self.values[key] for key in ("user_id", "request_id", "provider_id", "asset_id")})
        payload = public_resource_descriptor(descriptor)
        self.assertFalse(payload["inspectable"])
        self.assertNotIn("url", payload)

    def test_64_token_usage_is_bounded(self):
        token = self._token()
        binding = {key: self.values[key] for key in ("user_id", "request_id", "provider_id", "asset_id")}
        for _index in range(RESOURCE_MAX_USES):
            consume_resource_token(token=token, **binding)
        with self.assertRaises(ProviderLabError) as context:
            consume_resource_token(token=token, **binding)
        self.assertEqual(context.exception.code, "PROVIDER_RATE_LIMITED")
