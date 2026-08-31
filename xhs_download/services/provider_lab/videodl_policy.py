from __future__ import annotations

from dataclasses import dataclass


VIDEODL_POLICY_PIN = "6cd1de0fe1209380b183b654c65e98a629637873"
VIDEODL_POLICY_REVISION = "dedicated-v1-common-v1"
CONTROLLED_STATES = frozenset(
    {"allowed", "requires_sharing_confirmation", "blocked"}
)


@dataclass(frozen=True)
class VideodlBackendPolicy:
    backend_id: str
    controlled_state: str
    external_sharing_required: bool
    browser_execution: bool
    policy_reason: str

    def __post_init__(self) -> None:
        if self.controlled_state not in CONTROLLED_STATES:
            raise ValueError("invalid controlled_state")
        if (
            self.controlled_state == "allowed"
            and self.external_sharing_required
        ) or (
            self.controlled_state == "requires_sharing_confirmation"
            and not self.external_sharing_required
        ):
            raise ValueError("external sharing policy is inconsistent")
        if self.browser_execution and self.controlled_state != "blocked":
            raise ValueError("browser execution must be blocked")


# Exact-pin audit allowlist. A backend absent from this tuple is not reviewed and
# therefore cannot execute in a controlled mode.
REVIEWED_DEDICATED_BACKEND_IDS = (
    "ABCVideoClient",
    "AcFunVideoClient",
    "ArteTVVideoClient",
    "BaiduTiebaVideoClient",
    "BeaconVideoClient",
    "BilibiliVideoClient",
    "C56VideoClient",
    "CCCVideoClient",
    "CCtalkVideoClient",
    "DongchediVideoClient",
    "DouyinVideoClient",
    "CCTVNewsVideoClient",
    "DuxiaoshiVideoClient",
    "EyepetizerVideoClient",
    "FoxNewsVideoClient",
    "GeniusVideoClient",
    "HaokanVideoClient",
    "HuyaVideoClient",
    "IQiyiVideoClient",
    "KakaoVideoClient",
    "Ku6VideoClient",
    "KuaishouVideoClient",
    "KugouMVVideoClient",
    "M1905VideoClient",
    "MGTVVideoClient",
    "MeipaiVideoClient",
    "OasisVideoClient",
    "Open163VideoClient",
    "PearVideoClient",
    "PipigaoxiaoVideoClient",
    "PipixVideoClient",
    "PlayerPLVideoClient",
    "IYFVideoClient",
    "RedditVideoClient",
    "RednoteVideoClient",
    "SinaVideoClient",
    "SixRoomVideoClient",
    "SohuVideoClient",
    "TBNUKVideoClient",
    "TedVideoClient",
    "TencentVideoClient",
    "UnityVideoClient",
    "WWEVideoClient",
    "WeSingVideoClient",
    "WeiboVideoClient",
    "WeishiVideoClient",
    "WittyTVVideoClient",
    "XiguaVideoClient",
    "XinpianchangVideoClient",
    "XuexiCNVideoClient",
    "YinyuetaiVideoClient",
    "YouTubeVideoClient",
    "YoukuVideoClient",
    "ZhihuVideoClient",
    "ZuiyouVideoClient",
    "LeshiVideoClient",
    "KanKanNewsVideoClient",
    "NuVidVideoClient",
    "ChinaDailyVideoClient",
    "OrientalDailyVideoClient",
    "CCTVVideoClient",
    "WWW163VideoClient",
    "XinhuaNetVideoClient",
    "PeopleVideoClient",
    "HuanQiuVideoClient",
    "MingpaoVideoClient",
    "DailyMotionVideoClient",
    "RutubeVideoClient",
    "NewsPicksVideoClient",
    "MyVideoGeVideoClient",
)

_REQUIRES_SHARING = {
    "C56VideoClient": "NESTED_PROVIDER_SHARING_CONFIRMATION_REQUIRED",
}
_BLOCKED = {
    "DailyMotionVideoClient": (
        "EMBEDDED_PROVIDER_CREDENTIAL_BLOCKED",
        False,
        False,
    ),
    "HaokanVideoClient": ("EMBEDDED_COOKIE_BLOCKED", False, False),
    "KakaoVideoClient": ("EMBEDDED_COOKIE_BLOCKED", False, False),
    "KuaishouVideoClient": ("BROWSER_EXECUTION_BLOCKED", True, False),
    "MGTVVideoClient": ("BROWSER_EXECUTION_BLOCKED", True, False),
    "TencentVideoClient": ("CHILD_PROCESS_EXECUTION_BLOCKED", False, False),
    "XinpianchangVideoClient": ("BROWSER_EXECUTION_BLOCKED", True, False),
    "YinyuetaiVideoClient": ("TLS_VERIFICATION_DISABLED_BLOCKED", False, False),
    "YouTubeVideoClient": (
        "NESTED_AGGREGATOR_TLS_BYPASS_BLOCKED",
        False,
        True,
    ),
}


def _dedicated_policy(backend_id: str) -> VideodlBackendPolicy:
    if backend_id not in REVIEWED_DEDICATED_BACKEND_IDS:
        return VideodlBackendPolicy(
            backend_id=backend_id,
            controlled_state="blocked",
            external_sharing_required=False,
            browser_execution=False,
            policy_reason="UNREVIEWED_BACKEND_BLOCKED",
        )
    if backend_id in _BLOCKED:
        reason, browser_execution, external_sharing = _BLOCKED[backend_id]
        return VideodlBackendPolicy(
            backend_id=backend_id,
            controlled_state="blocked",
            external_sharing_required=external_sharing,
            browser_execution=browser_execution,
            policy_reason=reason,
        )
    if backend_id in _REQUIRES_SHARING:
        return VideodlBackendPolicy(
            backend_id=backend_id,
            controlled_state="requires_sharing_confirmation",
            external_sharing_required=True,
            browser_execution=False,
            policy_reason=_REQUIRES_SHARING[backend_id],
        )
    return VideodlBackendPolicy(
        backend_id=backend_id,
        controlled_state="allowed",
        external_sharing_required=False,
        browser_execution=False,
        policy_reason="EXACT_PIN_REVIEWED",
    )


DEDICATED_BACKEND_POLICIES = {
    backend_id: _dedicated_policy(backend_id)
    for backend_id in REVIEWED_DEDICATED_BACKEND_IDS
}


# Common backends always disclose the source reference to an external service.
# This exact-pin allowlist records which services may run after explicit Staff
# confirmation; an ID absent from the tuple is unreviewed and stays blocked.
REVIEWED_COMMON_BACKEND_IDS = (
    "SnapAnyVideoClient",
    "GVVideoClient",
    "SnapWCVideoClient",
    "IM1907VideoClient",
    "XMFlvVideoClient",
    "IIILabVideoClient",
    "RayVideoClient",
    "VeedMateVideoClient",
    "VideoFKVideoClient",
    "VThreadsVideoClient",
    "SENJiexiVideoClient",
    "JXM3U8VideoClient",
    "VgetVideoClient",
    "AnyFetcherVideoClient",
    "PVVideoClient",
    "KedouVideoClient",
    "ODwonVideoClient",
    "KuKuToolVideoClient",
    "APICXVideoClient",
    "SpapiVideoClient",
    "QingQiuVideoClient",
    "WzjunVideoClient",
    "XiaolvfangVideoClient",
    "BVVideoClient",
    "KuLeuVideoClient",
    "KIT9VideoClient",
    "MiZhiVideoClient",
    "QZXDPToolsVideoClient",
    "BugPkVideoClient",
    "NoLogoVideoClient",
    "GVVIPVideoClient",
    "JisuYunVideoClient",
    "QingtingVideoClient",
    "XCVTSVideoClient",
    "XiazaitoolVideoClient",
    "ZanqianbaVideoClient",
    "LongZhuVideoClient",
)

_COMMON_BLOCKED = {
    "APICXVideoClient": ("EMBEDDED_PROVIDER_CREDENTIAL_BLOCKED", False),
    "BugPkVideoClient": ("TLS_VERIFICATION_DISABLED_BLOCKED", False),
    "GVVIPVideoClient": ("BROWSER_EXECUTION_BLOCKED", True),
    "IIILabVideoClient": ("EMBEDDED_PROVIDER_CREDENTIAL_BLOCKED", False),
    "IM1907VideoClient": ("INTERACTIVE_EXECUTION_BLOCKED", False),
    "LongZhuVideoClient": ("TLS_VERIFICATION_DISABLED_BLOCKED", False),
    "QingtingVideoClient": ("EMBEDDED_PROVIDER_CREDENTIAL_BLOCKED", False),
    "SnapAnyVideoClient": ("EMBEDDED_PROVIDER_CREDENTIAL_BLOCKED", False),
    "SpapiVideoClient": ("EMBEDDED_PROVIDER_CREDENTIAL_BLOCKED", False),
    "VThreadsVideoClient": ("PROVIDER_DOWNLOADER_EXECUTION_BLOCKED", False),
    "VeedMateVideoClient": ("PROVIDER_DOWNLOADER_EXECUTION_BLOCKED", False),
    "VideoFKVideoClient": ("EMBEDDED_COOKIE_BLOCKED", False),
    "WzjunVideoClient": ("EMBEDDED_PROVIDER_CREDENTIAL_BLOCKED", False),
    "XCVTSVideoClient": ("EMBEDDED_PROVIDER_CREDENTIAL_BLOCKED", False),
    "XiazaitoolVideoClient": ("EMBEDDED_PROVIDER_CREDENTIAL_BLOCKED", False),
    "ZanqianbaVideoClient": ("EMBEDDED_COOKIE_BLOCKED", False),
}


def _common_policy(backend_id: str) -> VideodlBackendPolicy:
    if backend_id not in REVIEWED_COMMON_BACKEND_IDS:
        return VideodlBackendPolicy(
            backend_id=backend_id,
            controlled_state="blocked",
            external_sharing_required=False,
            browser_execution=False,
            policy_reason="UNREVIEWED_COMMON_BACKEND_BLOCKED",
        )
    blocked = _COMMON_BLOCKED.get(backend_id)
    if blocked is not None:
        reason, browser_execution = blocked
        return VideodlBackendPolicy(
            backend_id=backend_id,
            controlled_state="blocked",
            external_sharing_required=True,
            browser_execution=browser_execution,
            policy_reason=reason,
        )
    return VideodlBackendPolicy(
        backend_id=backend_id,
        controlled_state="requires_sharing_confirmation",
        external_sharing_required=True,
        browser_execution=False,
        policy_reason="COMMON_EXTERNAL_SHARING_CONFIRMATION_REQUIRED",
    )


COMMON_BACKEND_POLICIES = {
    backend_id: _common_policy(backend_id)
    for backend_id in REVIEWED_COMMON_BACKEND_IDS
}


def backend_policy(
    backend_id: str,
    backend_category: str,
    *,
    registry_present: bool = False,
) -> VideodlBackendPolicy:
    del registry_present
    if backend_category == "dedicated":
        return DEDICATED_BACKEND_POLICIES.get(
            backend_id,
            _dedicated_policy(backend_id),
        )
    if backend_category == "common":
        return COMMON_BACKEND_POLICIES.get(
            backend_id,
            _common_policy(backend_id),
        )
    return VideodlBackendPolicy(
        backend_id=backend_id,
        controlled_state="blocked",
        external_sharing_required=False,
        browser_execution=False,
        policy_reason="UNREVIEWED_BACKEND_BLOCKED",
    )


if (
    len(REVIEWED_DEDICATED_BACKEND_IDS) != 70
    or len(set(REVIEWED_DEDICATED_BACKEND_IDS)) != 70
    or set(REVIEWED_DEDICATED_BACKEND_IDS) != set(DEDICATED_BACKEND_POLICIES)
):
    raise RuntimeError("videodl exact-pin policy must cover 70 dedicated backends")

if (
    len(REVIEWED_COMMON_BACKEND_IDS) != 37
    or len(set(REVIEWED_COMMON_BACKEND_IDS)) != 37
    or set(REVIEWED_COMMON_BACKEND_IDS) != set(COMMON_BACKEND_POLICIES)
    or not set(_COMMON_BLOCKED).issubset(REVIEWED_COMMON_BACKEND_IDS)
):
    raise RuntimeError("videodl exact-pin policy must cover 37 common backends")
