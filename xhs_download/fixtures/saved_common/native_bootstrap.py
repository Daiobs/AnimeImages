"""Synthetic videofetch 0.9.1 ABI fixture, NOT an installed-pin attestation.

Only package metadata, registry/builders and the native parse boundary are
replaced. The production Worker file/main and JSON protocol run unchanged.
Registry names are frozen at the reviewed pin, not derived from live policy.
"""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import runpy
import socket
import sys
from types import ModuleType, SimpleNamespace

PIN = "6cd1de0fe1209380b183b654c65e98a629637873"
IDENTITY = "synthetic-videofetch-0.9.1:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
DEDICATED = """
ABC AcFun ArteTV BaiduTieba Beacon Bilibili C56 CCC CCtalk Dongchedi Douyin
CCTVNews Duxiaoshi Eyepetizer FoxNews Genius Haokan Huya IQiyi Kakao Ku6
Kuaishou KugouMV M1905 MGTV Meipai Oasis Open163 Pear Pipigaoxiao Pipix
PlayerPL IYF Reddit Rednote Sina SixRoom Sohu TBNUK Ted Tencent Unity WWE
WeSing Weibo Weishi WittyTV Xigua Xinpianchang XuexiCN Yinyuetai YouTube
Youku Zhihu Zuiyou Leshi KanKanNews NuVid ChinaDaily OrientalDaily CCTV
WWW163 XinhuaNet People HuanQiu Mingpao DailyMotion Rutube NewsPicks MyVideoGe
""".split()
COMMON = """
SnapAny GV SnapWC IM1907 XMFlv IIILab Ray VeedMate VideoFK VThreads SENJiexi
JXM3U8 Vget AnyFetcher PV Kedou ODwon KuKuTool APICX Spapi QingQiu Wzjun
Xiaolvfang BV KuLeu KIT9 MiZhi QZXDPTools BugPk NoLogo GVVIP JisuYun Qingting
XCVTS Xiazaitool Zanqianba LongZhu
""".split()


class OfflineViolation(BaseException):
    pass


def deny(*args, **kwargs):
    raise OfflineViolation("saved-common fixture forbids network")


def audit(event, args):
    if event.startswith("socket.") and event != "socket.__new__":
        deny()
    if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
        deny()


sys.addaudithook(audit)
socket.getaddrinfo = deny
socket.socket.connect = deny


def record(event, **fields):
    print("SAVED_COMMON_NATIVE " + json.dumps({"event": event, "identity": IDENTITY, **fields}),
          file=sys.stderr, flush=True)


class Backend:
    @classmethod
    def belongto(cls, source):
        return False

    def __init__(self, config):
        self.backend_id = config["type"]
        assert config["maintain_session"] is False
        assert config["auto_set_proxies"] is False
        assert config["default_parse_cookies"] is None

    def parsefromurl(self, source, **kwargs):
        assert self.backend_id in {"GVVideoClient", "AnyFetcherVideoClient"}
        assert kwargs == {"request_overrides": {}}
        assert source.startswith("https://www.acfun.cn/v/ac")
        record("parse", backend=self.backend_id, source=source)
        return [SimpleNamespace(
            source=self.backend_id, title="Saved Common offline fixture",
            download_url="https://media.example.org/fixture.mp4", ext="mp4",
            with_valid_download_url=True, requires_merge=False, audio_asset_present=True,
            default_download_headers={}, default_download_cookies=None,
        )]


def build(config):
    return Backend(config)


package = ModuleType("videodl")
package.__license__ = "PolyForm-Noncommercial-1.0.0"
modules = ModuleType("videodl.modules")
modules.VideoClientBuilder = SimpleNamespace(
    REGISTERED_MODULES={name + "VideoClient": Backend for name in DEDICATED})
modules.CommonVideoClientBuilder = SimpleNamespace(
    REGISTERED_MODULES={name + "VideoClient": Backend for name in COMMON})
modules.BuildVideoClient = build
modules.BuildCommonVideoClient = build
client = ModuleType("videodl.videodl")
client.VideoClient = Backend
sys.modules.update({"videodl": package, "videodl.modules": modules, "videodl.videodl": client})
original_version = importlib.metadata.version
original_distribution = importlib.metadata.distribution
importlib.metadata.version = lambda name: "0.9.1" if name == "videofetch" else original_version(name)
importlib.metadata.distribution = lambda name: (
    SimpleNamespace(read_text=lambda _: json.dumps({"vcs_info": {"commit_id": PIN}}))
    if name == "videofetch" else original_distribution(name))
record("identity")
target, *arguments = sys.argv[1:]
sys.path.insert(0, str(Path(target).parent))
sys.argv = [target, *arguments]
runpy.run_path(target, run_name="__main__")
