"""Adapter foundation boundaries; no real Provider or application request runs."""
import ast
from contextlib import ExitStack, contextmanager
from pathlib import Path
import socket
from unittest import TestCase
from unittest.mock import Mock, patch

from xhs_download.services.pipeline import (
    Failure, FailureKind, Fetchable, Item, Kind, Tracks, Variant, Work,
    fact_gaps, height_from_label, invoke, validate_work,
)
from xhs_download.services.provider_adapters import Adapter, ProviderAdapter, RawMapper
from xhs_download.services.provider_adapters.failures import (
    failure_for_code, failure_for_http,
)


ADAPTER_DIR = Path(__file__).parent / "services" / "provider_adapters"
FORBIDDEN = {
    "provider_strategy", "quality", "downloads", "download_handoff",
    "provider_manifest", "xhs_livephoto_pair_handoff", "upstream_delivery",
}


def forbidden_imports(source, module_name, *, package=False):
    """Resolve relative imports too, so spelling alone cannot bypass the boundary."""
    violations = []
    parent = module_name if package else module_name.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                prefix = parent.split(".")[:len(parent.split(".")) - node.level + 1]
                base = ".".join(prefix + ([base] if base else []))
            names = [base] + [f"{base}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            dynamic_import = (
                isinstance(function, ast.Name) and function.id == "__import__"
                or isinstance(function, ast.Attribute) and function.attr == "import_module"
            )
            if dynamic_import and isinstance(node.args[0], ast.Constant):
                names = [str(node.args[0].value)]
        for name in names:
            parts = name.split(".")
            if (any(part in FORBIDDEN or part.endswith("views") for part in parts)
                    or "platforms" in parts and any(
                        word in part for part in parts[parts.index("platforms") + 1:]
                        for word in ("policy", "authority", "admission"))):
                violations.append((node.lineno, name))
    return violations


@contextmanager
def deny_map_io():
    """Keep pure-map checks reusable by the later family fixture tests."""
    with ExitStack() as stack:
        for target in (
            "socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
            "socket.getaddrinfo", "socket.socket.sendto", "urllib.request.urlopen",
            "http.client.HTTPConnection.connect", "requests.sessions.Session.request",
            "subprocess.Popen", "builtins.open", "pathlib.Path.open",
            "time.time", "time.monotonic", "time.sleep",
        ):
            stack.enter_context(patch(target, side_effect=AssertionError("MAP_IO_FORBIDDEN")))
        yield


class FixtureAdapter(Adapter[dict]):
    name = "fixture"
    platforms = ("fixture",)

    def __init__(self, raw):
        self.raw = raw
        self.fetch_calls = []
        self.map_calls = []

    def fetch(self, source_url, budget):
        self.fetch_calls.append((source_url, budget))
        return self.raw

    def map(self, raw, source_url):
        self.map_calls.append((raw, source_url))
        variant = Variant(
            "v1", raw["url"], container=raw["container"],
            quality_label=raw["quality"], height=height_from_label(raw["quality"]),
            host_policy=("cdn.example",), browser_fetchable=Fetchable.UNKNOWN,
        )
        return Work(source_url, raw["id"], (Item("i1", Kind.VIDEO, 1, (variant,)),),
                    title=raw["title"], field_sources={"title": "provider", "quality": "provider"})


class AdapterFoundationTests(TestCase):
    def setUp(self):
        self.raw = {"id": "work-1", "url": "https://cdn.example/1.mp4",
                    "container": "mp4", "quality": "1080P+", "title": "Fixture title"}
        self.url = "https://source.example/work/1"

    def test_protocol_and_single_fetch_map_compose_without_old_results(self):
        adapter = FixtureAdapter(self.raw)
        self.assertIsInstance(adapter, ProviderAdapter)
        result = invoke(adapter, self.url, 2.5)
        self.assertEqual(adapter.fetch_calls, [(self.url, 2.5)])
        self.assertEqual(adapter.map_calls, [(self.raw, self.url)])
        self.assertIs(adapter.map_calls[0][0], self.raw)
        self.assertEqual(validate_work(result), ())
        self.assertEqual((result.provider, result.platform), ("", ""))
        variant = result.items[0].default_variant
        self.assertEqual(variant.height, 1080)
        self.assertIs(variant.tracks, Tracks.UNKNOWN)
        self.assertEqual(fact_gaps(result), ("TRACKS_UNKNOWN",))

    def test_fetch_failure_skips_map_and_never_retries(self):
        failure = Failure(FailureKind.UNAVAILABLE, "HTTP_412")
        adapter = FixtureAdapter(failure)
        self.assertIs(invoke(adapter, self.url, 2), failure)
        self.assertEqual(len(adapter.fetch_calls), 1)
        self.assertEqual(adapter.map_calls, [])

    def test_map_only_worker_family_does_not_claim_fetch_capability(self):
        class MapOnly:
            name = "fixture_worker"
            platforms = ("fixture",)
            map = FixtureAdapter(self.raw).map
        mapper = MapOnly()
        self.assertIsInstance(mapper, RawMapper)
        self.assertNotIsInstance(mapper, ProviderAdapter)
        with deny_map_io():
            result = mapper.map(self.raw, self.url)
        self.assertEqual(validate_work(result), ())

    def test_map_failure_passes_through(self):
        adapter = FixtureAdapter(self.raw)
        failure = Failure(FailureKind.INVALID_RESULT, "MISSING_MEDIA")
        adapter.map = Mock(return_value=failure)
        self.assertIs(invoke(adapter, self.url, 2), failure)
        adapter.map.assert_called_once_with(self.raw, self.url)

    def test_invoke_is_the_existing_exception_boundary(self):
        for stage in ("fetch", "map"):
            with self.subTest(stage=stage):
                adapter = FixtureAdapter(self.raw)
                setattr(adapter, stage, Mock(side_effect=ValueError("private-response-value")))
                result = invoke(adapter, self.url, 2)
                self.assertEqual(result, Failure(FailureKind.INVALID_RESULT, "ADAPTER_EXCEPTION"))
                self.assertNotIn("private", repr(result))
                self.assertLessEqual(len(adapter.fetch_calls), 1)

    def test_invalid_budget_does_not_fetch(self):
        for budget in (0, -1, float("nan"), float("inf")):
            with self.subTest(budget=budget):
                adapter = FixtureAdapter(self.raw)
                self.assertEqual(invoke(adapter, self.url, budget),
                                 Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED"))
                self.assertEqual(adapter.fetch_calls, [])

    def test_invalid_map_output_is_not_a_second_result_model(self):
        adapter = FixtureAdapter(self.raw)
        adapter.map = Mock(return_value={"legacy_assets": []})
        self.assertEqual(invoke(adapter, self.url, 2),
                         Failure(FailureKind.INVALID_RESULT, "ADAPTER_INVALID_OUTPUT"))

    def test_fetch_exhausting_budget_skips_map(self):
        adapter = FixtureAdapter(self.raw)
        with patch("time.monotonic", side_effect=[10, 12]):
            result = invoke(adapter, self.url, 2)
        self.assertEqual(result, Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED"))
        self.assertEqual(adapter.map_calls, [])
        self.assertEqual(adapter.fetch_calls, [(self.url, 2)])

    def test_mapping_time_is_included_in_total_budget(self):
        adapter = FixtureAdapter(self.raw)
        with patch("time.monotonic", side_effect=[10, 11, 12.1]):
            result = invoke(adapter, self.url, 2)
        self.assertEqual(result, Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED"))
        self.assertEqual(len(adapter.map_calls), 1)
        self.assertEqual(len(adapter.fetch_calls), 1)

    def test_map_has_no_io_and_does_not_modify_raw(self):
        raw = dict(self.raw)
        adapter = FixtureAdapter(raw)
        with deny_map_io():
            first = adapter.map(raw, self.url)
            second = adapter.map(raw, self.url)
        self.assertEqual(first, second)
        self.assertEqual(raw, self.raw)
        self.assertEqual(adapter.fetch_calls, [])

    def test_network_guard_counterexample(self):
        with deny_map_io(), self.assertRaisesRegex(AssertionError, "MAP_IO_FORBIDDEN"):
            socket.create_connection(("cdn.example", 443))

    def test_all_adapter_modules_obey_import_boundary(self):
        for path in ADAPTER_DIR.rglob("*.py"):
            module = ".".join(path.relative_to(Path(__file__).parent.parent).with_suffix("").parts)
            package = path.name == "__init__.py"
            if package:
                module = module.rsplit(".", 1)[0]
            with self.subTest(module=module):
                self.assertEqual(forbidden_imports(path.read_text(), module, package=package), [])

    def test_import_guard_counterexamples(self):
        imports = (
            "from .. import provider_strategy", "from ..quality import QualityProbe",
            "from ..downloads import download", "from ..download_handoff import prepare",
            "from ..provider_manifest import issue", "from ..xhs_livephoto_pair_handoff import prepare",
            "from ..upstream_delivery import normalize", "from ... import views",
            "from xhs_download.ytdlp_selection_views import view",
            "from ..platforms.xhs_tiantian_media_policy import policy",
            "from ..platforms.xhs_image_download_authority import authorize",
            "from ..platforms.xhs_livephoto_browser_admission import admit",
            "importlib.import_module('xhs_download.services.downloads')",
        )
        for source in imports:
            with self.subTest(source=source):
                self.assertTrue(forbidden_imports(source, "xhs_download.services.provider_adapters.example"))
        self.assertEqual(forbidden_imports(
            "from ..pipeline import Work\nfrom ..platforms.ytdlp_generic import run_ytdlp_json",
            "xhs_download.services.provider_adapters.example"), [])


class AdapterFailureTests(TestCase):
    def test_http_failure_mapping(self):
        for status, kind in ((404, FailureKind.NOT_FOUND), (410, FailureKind.NOT_FOUND),
                             (408, FailureKind.TIMEOUT), (412, FailureKind.UNAVAILABLE),
                             (429, FailureKind.UNAVAILABLE), (500, FailureKind.UNAVAILABLE),
                             (503, FailureKind.UNAVAILABLE), (504, FailureKind.UNAVAILABLE),
                             (401, FailureKind.UNAVAILABLE), (403, FailureKind.UNAVAILABLE),
                             (400, FailureKind.INVALID_RESULT), (302, FailureKind.INVALID_RESULT)):
            with self.subTest(status=status):
                self.assertEqual(failure_for_http(status), Failure(kind, f"HTTP_{status}"))
        for status in (200, 204, 206):
            self.assertIsNone(failure_for_http(status))

    def test_business_failure_table_is_supplied_by_family(self):
        missing = Failure(FailureKind.NOT_FOUND, "WORK_DELETED")
        rules = {"1001": Failure(FailureKind.UNAVAILABLE, "SERVICE_UNAVAILABLE"), "gone": missing}
        self.assertEqual(failure_for_code(1001, rules), rules["1001"])
        self.assertIs(failure_for_code("gone", rules), missing)
        self.assertEqual(failure_for_code("secret unknown response", rules),
                         Failure(FailureKind.UNAVAILABLE, "UNMAPPED_UPSTREAM_CODE"))

    def test_no_raw_error_message_or_invalid_status_in_diagnostic(self):
        for status in ("https://private.example/?token=value", True, 99, 600):
            with self.subTest(status=type(status).__name__), self.assertRaises(ValueError):
                failure_for_http(status)
