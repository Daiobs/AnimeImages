"""Unified pipeline §4: filenames from the unified result (INV-06)."""
from unittest import TestCase

from xhs_download.services.pipeline import (
    MAX_BASE_LENGTH,
    Group,
    Item,
    Kind,
    UnknownExtension,
    Work,
    archive_name,
    base_name,
    file_name,
    item_numbers,
)

from .fakes import SOURCE, variant, video_work


def gallery(*items, groups=(), title="春日合集"):
    return Work(source_url=SOURCE, work_id="w42", platform="example", title=title,
                items=tuple(items), groups=groups)


def image(item_id, index, container="jpg"):
    return Item(item_id=item_id, kind=Kind.IMAGE, index=index,
                variants=(variant(f"{item_id}-v", container=container),))


def motion(item_id, index):
    return Item(item_id=item_id, kind=Kind.VIDEO, index=index,
                variants=(variant(f"{item_id}-v", container="mov"),))


class FilenameContract(TestCase):
    def test_base_name_is_the_short_title_cleaned_and_capped(self):
        self.assertEqual(base_name(video_work(title=' 雨夜/街角: "霓虹" ')), "雨夜 街角 霓虹")
        long_title = "长" * (MAX_BASE_LENGTH + 20)
        self.assertEqual(len(base_name(video_work(title=long_title))), MAX_BASE_LENGTH)
        self.assertEqual(MAX_BASE_LENGTH, 80)

    def test_without_a_title_the_name_is_platform_and_work_id(self):
        work = video_work(title="", platform="example", work_id="w42",
                          description="正文不进入文件名", topics=("话题",))
        self.assertEqual(base_name(work), "example_w42")

    def test_description_and_topics_never_enter_the_name(self):
        work = video_work(title="短标题", description="很长的正文", topics=("话题一", "话题二"))
        name = file_name(work, work.items[0], work.items[0].variants[0])
        self.assertEqual(name, "短标题.mp4")

    def test_several_items_are_numbered_by_index(self):
        work = gallery(image("c", 3), image("a", 1), image("b", 2))
        self.assertEqual(item_numbers(work), {"a": 1, "b": 2, "c": 3})
        self.assertEqual(file_name(work, work.items[0], work.items[0].variants[0]), "春日合集_3.jpg")

    def test_live_photo_pair_shares_one_number(self):
        work = gallery(image("s1", 1), motion("m1", 2), image("s2", 3), motion("m2", 4),
                       groups=(Group("live_photo", ("s1", "m1")), Group("live_photo", ("s2", "m2"))))
        self.assertEqual(item_numbers(work), {"s1": 1, "m1": 1, "s2": 2, "m2": 2})
        names = {file_name(work, item, item.variants[0]) for item in work.items}
        self.assertEqual(names, {"春日合集_1.jpg", "春日合集_1.mov", "春日合集_2.jpg", "春日合集_2.mov"})

    def test_a_single_unit_has_no_number(self):
        single = gallery(image("s1", 1), motion("m1", 2), groups=(Group("live_photo", ("s1", "m1")),))
        self.assertEqual(item_numbers(single), {"s1": None, "m1": None})
        self.assertEqual(item_numbers(video_work()), {"i1": None})

    def test_extension_is_real_never_a_placeholder(self):
        work = video_work()
        item = work.items[0]
        self.assertEqual(file_name(work, item, variant(container="JPEG")), "契约作品.jpg")
        self.assertEqual(file_name(work, item, variant(container="bin"), "video/webm"), "契约作品.webm")
        self.assertEqual(file_name(work, item, variant(container=""), "audio/mp4; codecs=mp4a"),
                         "契约作品.m4a")
        for container, content_type in (("", ""), ("bin", "application/octet-stream"),
                                        ("", "text/html")):
            with self.subTest(container=container, content_type=content_type):
                with self.assertRaises(UnknownExtension):
                    file_name(work, item, variant(container=container), content_type)

    def test_archive_uses_the_same_base_name(self):
        self.assertEqual(archive_name(gallery(image("a", 1), image("b", 2))), "春日合集.zip")
