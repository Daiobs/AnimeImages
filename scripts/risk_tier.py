"""Classify changed paths using the repository's single risk manifest."""

import argparse
import fnmatch
import html
import json
from pathlib import Path


TIERS = ("L0", "L1", "L2")
DEFAULT_RULES = Path(__file__).resolve().parents[1] / ".github/risk-tiers.json"


def load_rules(path=DEFAULT_RULES):
    rules = json.loads(Path(path).read_text(encoding="utf-8"))
    if rules.get("version") != 1 or rules.get("default_tier") not in TIERS:
        raise ValueError("Invalid risk manifest")
    if set(rules.get("tiers", {})) != set(TIERS):
        raise ValueError("Risk manifest must define L0, L1 and L2")
    for tier in TIERS:
        patterns = rules["tiers"][tier].get("paths")
        if not isinstance(patterns, list) or not patterns or not all(
            isinstance(pattern, str) and pattern for pattern in patterns
        ):
            raise ValueError(f"Invalid paths for {tier}")
    return rules


def classify(paths, rules):
    matches = []
    for path in sorted(set(paths)):
        if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
            raise ValueError("Expected repository-relative file paths")
        hits = [
            (tier, pattern)
            for tier in TIERS
            for pattern in rules["tiers"][tier]["paths"]
            if fnmatch.fnmatchcase(path, pattern)
        ]
        tier = max((hit[0] for hit in hits), default=rules["default_tier"])
        matches.append({
            "path": path,
            "tier": tier,
            "patterns": [pattern for hit_tier, pattern in hits if hit_tier == tier],
        })
    tier = max((item["tier"] for item in matches), default="L0")
    return {"tier": tier, "matches": matches, "evidence": rules["tiers"][tier]["evidence"]}


def markdown_summary(result):
    rows = [
        f"## Risk tier: {result['tier']}",
        "",
        result["evidence"],
        "",
        "| Path | Tier | Matched rule |",
        "| --- | --- | --- |",
    ]
    for item in result["matches"]:
        # PR-controlled filenames are data, never commands or raw Markdown.
        def safe(value):
            text = html.escape(str(value), quote=True)
            for character, encoded in (
                ("|", "&#124;"), ("`", "&#96;"), ("\n", "&#10;"),
                ("\r", "&#13;"), ("[", "&#91;"), ("]", "&#93;"),
            ):
                text = text.replace(character, encoded)
            return text
        patterns = ", ".join(item["patterns"]) or "(default)"
        rows.append(f"| {safe(item['path'])} | {item['tier']} | {safe(patterns)} |")
    return "\n".join(rows) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--paths-json", type=Path, help="JSON list of changed paths")
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    paths = args.paths
    if args.paths_json:
        paths += json.loads(args.paths_json.read_text(encoding="utf-8"))
    result = classify(paths, load_rules(args.rules))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as output:
            output.write(markdown_summary(result))


if __name__ == "__main__":
    main()
