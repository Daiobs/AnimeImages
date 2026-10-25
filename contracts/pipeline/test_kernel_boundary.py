"""Unified pipeline §0 rule 1: the kernel knows no platform or Provider."""
import ast
from pathlib import Path
import re
from unittest import TestCase

import xhs_download.services.pipeline as kernel

KERNEL_DIR = Path(kernel.__file__).parent

# Names that would mean the kernel branches on where a result came from.
FORBIDDEN = re.compile(
    r"douyin|xhs|xiaohongshu|bilibili|youtube|tiktok|instagram|twitter|weibo|kuaishou"
    r"|tiantian|kukutool|parsehub|tikhub|gallery.?dl|yt.?dlp|videodl|native|upstream:",
    re.IGNORECASE,
)


class KernelBoundaryContract(TestCase):
    def kernel_sources(self):
        sources = sorted(KERNEL_DIR.glob("*.py"))
        self.assertGreater(len(sources), 1)
        return sources

    def test_no_platform_provider_or_resource_prefix_names(self):
        for path in self.kernel_sources():
            with self.subTest(path=path.name):
                self.assertIsNone(FORBIDDEN.search(path.read_text(encoding="utf-8")))

    def test_kernel_imports_only_the_standard_library_and_itself(self):
        for path in self.kernel_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    continue
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    with self.subTest(path=path.name, module=name):
                        self.assertNotIn(name.split(".")[0], {"xhs_download", "django", "requests"})
