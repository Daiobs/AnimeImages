from dataclasses import dataclass


@dataclass(frozen=True)
class MediaCandidate:
    url: str
    quality_label: str = "原始文件"
    size_bytes: int = 0
    width: int = 0
    height: int = 0
    bit_rate: int = 0
    ratio_score: int = 0
    priority: int = 0
    quality_type: int = 0
    source: str = ""

    @property
    def resolution_score(self):
        return max(self.width, self.height)

    @property
    def score(self):
        return (
            self.size_bytes,
            self.resolution_score,
            self.bit_rate,
            self.ratio_score,
            self.priority,
            self.quality_type,
        )


def dedupe_candidates(candidates):
    ordered = []
    seen = set()
    for candidate in candidates:
        if not candidate.url or candidate.url in seen:
            continue
        seen.add(candidate.url)
        ordered.append(candidate)
    return ordered


def rank_media_candidates(candidates):
    return dedupe_candidates(sorted(candidates, key=lambda item: item.score, reverse=True))
