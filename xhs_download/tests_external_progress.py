from django.test import SimpleTestCase

from .services.platforms.external_progress import (
    MAX_PROGRESS_BYTES,
    STRUCTURED_PROGRESS_COMPLETION_RATIO,
    YTDLP_PROGRESS_PREFIX,
    YTDLP_PROGRESS_TEMPLATE,
    YtDlpProgressTracker,
    normalized_ytdlp_progress,
    parse_ytdlp_progress_line,
    with_ytdlp_progress_arguments,
    ytdlp_progress_arguments,
)


def progress_line(
    status="downloading",
    downloaded="25",
    total="100",
    estimate="NA",
    fragment_index="NA",
    fragment_count="NA",
):
    return (
        f"{YTDLP_PROGRESS_PREFIX}{status}\t{downloaded}\t{total}\t{estimate}\t"
        f"{fragment_index}\t{fragment_count}"
    )


class YtDlpProgressParserTests(SimpleTestCase):
    def test_progress_template_has_closed_fields_and_prefix(self):
        self.assertTrue(YTDLP_PROGRESS_TEMPLATE.startswith("download:"))
        self.assertIn(YTDLP_PROGRESS_PREFIX, YTDLP_PROGRESS_TEMPLATE)
        self.assertEqual(len(ytdlp_progress_arguments()), 3)
        for field in (
            "status",
            "downloaded_bytes",
            "total_bytes",
            "total_bytes_estimate",
            "fragment_index",
            "fragment_count",
        ):
            self.assertIn(f"%(progress.{field})s", YTDLP_PROGRESS_TEMPLATE)

    def test_exact_byte_event_is_parsed(self):
        event = parse_ytdlp_progress_line(progress_line())
        self.assertEqual(event.status, "downloading")
        self.assertEqual(event.downloaded_bytes, 25)
        self.assertEqual(event.total_bytes, 100)
        self.assertIsNone(event.total_bytes_estimate)

    def test_estimated_total_is_used_when_exact_total_is_missing(self):
        event = parse_ytdlp_progress_line(
            progress_line(downloaded="20", total="NA", estimate="80")
        )
        self.assertEqual(normalized_ytdlp_progress(event), (20, 80))

    def test_fragment_progress_uses_existing_estimated_size(self):
        event = parse_ytdlp_progress_line(
            progress_line(
                downloaded="NA",
                total="NA",
                estimate="NA",
                fragment_index="2",
                fragment_count="4",
            )
        )
        self.assertEqual(
            normalized_ytdlp_progress(event, estimated_size=1000),
            (500, 1000),
        )

    def test_event_total_maps_to_existing_caller_estimate(self):
        event = parse_ytdlp_progress_line(
            progress_line(downloaded="25", total="50")
        )
        self.assertEqual(
            normalized_ytdlp_progress(event, estimated_size=1000),
            (500, 1000),
        )

    def test_finished_event_maps_to_selected_total(self):
        event = parse_ytdlp_progress_line(progress_line(status="finished"))
        self.assertEqual(
            normalized_ytdlp_progress(event, estimated_size=1000),
            (1000, 1000),
        )

    def test_invalid_or_unbounded_records_fail_closed(self):
        invalid = (
            "not-progress",
            f"{YTDLP_PROGRESS_PREFIX}unknown\t1\t2\t3\t4\t5",
            f"{YTDLP_PROGRESS_PREFIX}downloading\t1\t2",
            progress_line(downloaded="-1"),
            progress_line(downloaded="nan"),
            progress_line(downloaded=str(MAX_PROGRESS_BYTES + 1)),
        )
        for line in invalid[:3]:
            with self.subTest(line=line):
                self.assertIsNone(parse_ytdlp_progress_line(line))
        for line in invalid[3:]:
            with self.subTest(line=line):
                self.assertIsNone(parse_ytdlp_progress_line(line).downloaded_bytes)

    def test_progress_arguments_are_inserted_before_source_once(self):
        source = "https://video.example/watch/fixture"
        args = with_ytdlp_progress_arguments(["yt-dlp", "-o", "out", source])
        self.assertEqual(args[-1], source)
        self.assertEqual(args.count("--progress-template"), 1)
        self.assertEqual(with_ytdlp_progress_arguments(args), args)


class YtDlpProgressTrackerTests(SimpleTestCase):
    def test_tracker_emits_monotonic_bounded_progress(self):
        events = []
        tracker = YtDlpProgressTracker(
            lambda completed, total: events.append((completed, total)),
            estimated_size=1000,
        )
        tracker.emit_initial()
        tracker.consume_line(progress_line(downloaded="50", total="100"))
        tracker.consume_line(progress_line(downloaded="25", total="100"))
        tracker.consume_line(progress_line(status="finished"))
        tracker.emit_finished(1100)
        self.assertEqual(events[0], (20, 1000))
        self.assertEqual(events[-1], (1100, 1100))
        self.assertEqual(
            [item[0] for item in events],
            sorted(item[0] for item in events),
        )
        self.assertLessEqual(
            events[-2][0],
            int(events[-2][1] * STRUCTURED_PROGRESS_COMPLETION_RATIO),
        )

    def test_tracker_callback_accepts_existing_two_argument_contract(self):
        events = []
        tracker = YtDlpProgressTracker(
            lambda completed, total: events.append((completed, total)),
            estimated_size=200,
        )
        tracker.consume_line(progress_line(downloaded="10", total="20"))
        self.assertEqual(events, [(100, 200)])

    def test_structured_event_disables_directory_fallback(self):
        events = []
        tracker = YtDlpProgressTracker(
            lambda completed, total: events.append((completed, total)),
            estimated_size=100,
        )
        tracker.emit_directory_fallback(10, 100)
        tracker.consume_line(progress_line(downloaded="50", total="100"))
        tracker.emit_directory_fallback(90, 100)
        self.assertEqual(events[-1], (50, 100))

    def test_directory_progress_remains_when_no_structured_event_arrives(self):
        events = []
        tracker = YtDlpProgressTracker(
            lambda completed, total: events.append((completed, total)),
            estimated_size=100,
        )
        tracker.emit_directory_fallback(30, 100)
        tracker.emit_directory_fallback(20, 100)
        self.assertEqual(events, [(30, 100)])

    def test_callback_error_is_not_swallowed(self):
        def fail(_completed, _total):
            raise RuntimeError("progress failed")

        tracker = YtDlpProgressTracker(fail, estimated_size=100)
        with self.assertRaisesRegex(RuntimeError, "progress failed"):
            tracker.consume_line(progress_line())
