# ParseHub Architecture Map

This map is bound to ParseHub commit
`144260f4349c5bb57415f7555a572fcd48c3b8ba`. It is a static supply review, not
a runtime endorsement.

## Layer map

| Layer | Responsibility and public surface | Network/state/dependencies | AnimeImages equivalent | Reuse type |
| --- | --- | --- | --- | --- |
| ParseHub facade | `src/parsehub/__init__.py`; selects parser, exposes async/sync parse and download, lists platforms | Loads registry; passes proxy/Cookie; result serialization contains raw/media URLs | `services/registry.py` plus controlled views/services | `DO_NOT_REUSE` |
| BaseParser registry | `parsers/base/base.py`; subclass registry, `pkgutil` module walk, regex URL matching, redirect resolution, query cleaning | Imports every parser module; redirect GET follows redirects with a fixed 30-second timeout | Explicit `PlatformRegistry` and `BasePlatformAdapter` | `CONCEPT` |
| Platform parsers | `parsers/parser/*.py`; map platform payloads into result types | Native API, HTML, hybrid or yt-dlp depending on platform | Dedicated adapters plus gated generic providers | `PLATFORM_KNOWLEDGE` |
| Provider API | `provider_api/*.py`; request/sign/normalize platform payloads | Platform endpoints, Cookies, signatures, HTML and API state | Dedicated Provider implementations behind policy and runtime gates | `PLATFORM_KNOWLEDGE` |
| Result and media references | `types/result.py`, `media_ref.py`, `post.py`; video, image, animation, LivePhoto, multimedia and rich text | Dataclasses/models retain direct URLs and optional headers | `MediaAsset`, `ParseResult`, provider manifests and view contexts | `DATA_MODEL` |
| yt-dlp base | `parsers/base/ytdlp.py`; subprocess extraction/download and progress parsing | Process groups, bounded output tails, temporary Cookie/info files, termination and cancellation | `external_tools.py`, `ytdlp_generic.py`, merge services | `CONCEPT` |
| Local downloader | `utils/downloader.py`; HEAD/Range probe, multipart transfer, retry, progress, atomic replace and integrity checks | Local/server HTTP data path and filesystem writes | Browser Streaming for ordinary media; server paths only for generated output | `DO_NOT_REUSE` |
| CLI | `cli.py`; parse, download, platform listing and configuration commands | Executes facade and prints JSON/human output | Django management/admin tools with permission gates | `TOOLING` |
| Config/Cookie store | `cli_config.py`, `config/config.py`; per-platform parse/download proxy and Cookie persistence | TOML under user config directory; Cookie file is chmod `0600` where supported | Environment capability plus Admin runtime gates; credentials not exposed to public UI | `CONCEPT` |
| Tests | `test/*.py`; core, CLI, config and downloader coverage | Offline mocks/fixtures are present; no tests were run here | Django and Node test suites | `TOOLING` |
| CI/PyPI | `.github/workflows/python-publish.yml`; lint, type check, test, build and trusted publish | GitHub Actions and PyPI release environment | AnimeImages GitHub workflows and controlled deploy flow | `CONCEPT` |

The BaseParser row intentionally does not recommend its dynamic registry. Its
only reusable idea is normalized query-parameter handling; the AnimeImages
explicit registry remains authoritative.

## Required module findings

### BaseParser registry

- **SOURCE-DERIVED FACT:** subclasses register into a process-global list via
  `__init_subclass__`; registry loading walks parser modules with `pkgutil` and
  imports them dynamically.
- **SOURCE-DERIVED FACT:** matching is regex-based after extracting the first
  URL-like value. Short-link keywords trigger a server-side redirect-following
  GET.
- **SOURCE-DERIVED FACT:** reserved parameters survive canonical cleaning and
  after-clean parameters may be used during parsing then removed from returned
  raw URLs.
- **ANIMEIMAGES FACT:** AnimeImages uses explicit adapters, validates scheme and
  admitted hostname, and blocks unsafe/private hostnames before parsing.
- **RECOMMENDATION:** retain the explicit registry. Never let discovery of an
  upstream parser make a public platform available automatically.

### Result and MediaRef model

- **SOURCE-DERIVED FACT:** the model expresses `VideoRef`, `ImageRef`, `AniRef`,
  `LivePhotoRef`, `MultimediaParseResult` and `RichTextParseResult`.
- **SOURCE-DERIVED FACT:** `LivePhotoRef` preserves image/video pairing; rich
  text can retain normalized Markdown/plain text; multimedia preserves ordered
  heterogeneous media.
- **ANIMEIMAGES FACT:** `MediaAsset` has stronger download provenance, size
  semantics, provider identity, cache identity and signed manifest fields, but
  lacks a first-class general pairing/rich-text contract.
- **RECOMMENDATION:** absorb concepts through a future AnimeImages-owned domain
  model. Never return ParseHub result objects through a view.

### Downloader

- **SOURCE-DERIVED FACT:** it probes HEAD, then a one-byte Range when necessary,
  supports multipart Range downloads, retries with backoff, reports progress,
  joins parts, validates expected size and atomically replaces the destination.
- **INFERENCE:** these are useful local-tooling algorithms, but the downloader
  has no AnimeImages media-host admission or public permission boundary.
- **ANIMEIMAGES FACT:** ordinary Provider media follows the zero-relay browser
  data plane; server download is reserved for generated output such as merge or
  ZIP tasks.
- **RECOMMENDATION:** reject direct integration into public runtime. Reference
  integrity ideas only for offline tooling or server-generated output.

### yt-dlp subprocess runtime

- **SOURCE-DERIVED FACT:** it starts yt-dlp in a process group, reads progress
  lines, keeps bounded stdout/stderr tails, materializes Cookie and info JSON in
  private temporary files, sends SIGTERM, waits five seconds and escalates to
  SIGKILL, and cleans temporary files on cancellation.
- **ANIMEIMAGES FACT:** AnimeImages already bounds command timeout, stdout/stderr
  file size, environment, temporary cwd/HOME, executor concurrency and provider
  budget. Its generic runner terminates the immediate process but does not yet
  implement the same explicit process-group lifecycle.
- **RECOMMENDATION:** this is the first adoption candidate, limited to an
  AnimeImages-owned subprocess lifecycle primitive with sanitized errors.

### CLI and configuration

- **SOURCE-DERIVED FACT:** parse and download proxies can be configured
  independently per platform. Cookies are prompted without shell echo and saved
  separately; the Cookie file is set to mode `0600` where supported.
- **SOURCE-DERIVED FACT:** parse/download can emit JSON and downloads may emit a
  metadata file containing result information.
- **SECURITY NOTE:** local file permissions reduce exposure but do not create an
  acceptable public credential boundary. Serialized results contain direct URLs.
- **RECOMMENDATION:** reference the separation of parse/download proxy intent;
  retain AnimeImages environment/Admin/runtime governance.

## Platform-specific findings

### Douyin

- **SOURCE-DERIVED FACT:** Web API is attempted when a Cookie is supplied, then
  a signed mobile API path can resolve/register devices and maintain a device
  pool. Video, image and Story/日常-related payload forms are represented.
- **SOURCE-DERIVED FACT:** quality selection considers dimensions, data size and
  bitrate, and supports image/video pairing for live-style media.
- **ANIMEIMAGES FACT:** AnimeImages has canonical aweme identity, bounded source
  resolution, Provider races, cache, single-flight, cooldown, attempt accounting,
  exact host admission, Browser Streaming and production evidence.
- **RECOMMENDATION:** retain AnimeImages. Story/mobile details are reference-only
  until a dedicated security and live-evidence task exists.

### Xiaohongshu

- **SOURCE-DERIVED FACT:** the parser reads page state, maps video/gallery/
  LivePhoto media, constructs raw-image candidates and may use HTTP metadata to
  infer an extension.
- **INFERENCE:** generic page extraction and URL rewriting need source-identity,
  false-positive and host-admission review before reuse.
- **ANIMEIMAGES FACT:** current-note authority, recommendation suppression,
  terminal-state precedence, raster signature checks and production gallery
  acceptance already exceed this baseline.
- **RECOMMENDATION:** do not replace the adapter. LivePhoto pairing remains a
  domain-model reference.

### Bilibili

- **SOURCE-DERIVED FACT:** native API handles video metadata and dynamic/opus
  content, including image/LivePhoto-like dynamic media; a yt-dlp path handles
  video extraction and supplies download headers.
- **ANIMEIMAGES FACT:** the current adapter intentionally supports baseline
  public MP4/cover parsing and avoids member-only DASH work.
- **RECOMMENDATION:** Bilibili Dynamic/LivePhoto is an adoption candidate, but it
  follows subprocess lifecycle hardening and needs fictional fixtures plus live
  staff-only proof in its own task.

### Kuaishou

- **SOURCE-DERIVED FACT:** native API and HTML/structured-data fallbacks exist,
  with multiple public-route candidates and Cookie/proxy support.
- **SOURCE-DERIVED FACT:** the registered parser source contains embedded
  Cookie-like session material. `EMBEDDED_SESSION_MATERIAL_PRESENT = YES`.
- **RECOMMENDATION:** no literal or implementation may be copied. Any future
  Kuaishou work starts with credential removal, endpoint/host review and an
  AnimeImages-owned adapter behind Staff-only gates.

## AnimeImages capabilities that remain authoritative

Explicit registry, host/IP admission, deadlines, payload cache, single-flight,
cooldown, rate limits, circuits, attempt accounting, manifests, Public/Staff
boundaries, Browser Streaming, zero relay and production acceptance are all
retained. ParseHub is a capability supplier and research source, not a runtime
governance authority.
