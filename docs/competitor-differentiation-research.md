# Competitor Differentiation Research

Date: 2026-06-07

Scope:

- Product differentiation for a universal no-watermark media downloader.
- Technical differentiation for Douyin ultra-HD source discovery.
- Short-term implementation guidance for the current Django project.

## Reference Targets

| Target | Role in research | Notes |
| --- | --- | --- |
| https://www.hellotik.app/zh | UI/product reference | Clean homepage, focused input flow, simple value cards. Live fetch was unstable during this pass, so UI observations should be rechecked in browser before another redesign. |
| https://www.hellotik.app/zh/rednote | Platform-specific page reference | Separate Rednote page suggests SEO/platform-specific copy while keeping the same core parsing experience. |
| https://snapany.com/zh | Product breadth reference | Positions itself around broad platform support, desktop app, browser extension, batch handling, subtitle/audio/merge workflows. |
| https://dy.kukutool.com/zh | Ultra-HD technical reference | Strongest visible evidence of multi-resolution output for Douyin-like links, with guarded server-side parse flow. |

## Current Product Position

The current project already has several useful differentiators:

- One universal input can route to platform adapters.
- Xiaohongshu image handling is strong because it preserves original file bytes by default.
- The result area is more useful than many competitors because it shows actual selectable media resources instead of only a single opaque download button.
- Local download tasks and progress are under our control, which is important for later batch UX.

The current weak point is source discovery depth:

- Douyin baseline can parse and save video, but some samples resolve only to smaller 480p/540p/720p/1080p sources.
- Xiaohongshu video baseline can miss larger source variants when competitors find much larger files.
- Bilibili baseline works, but ultra-HD is intentionally out of scope because it usually requires cookies, DASH merge, and entitlement handling.

## Competitor Product Patterns

### HelloTik Pattern

Useful lessons:

- Keep the homepage quiet and direct: title, short claim, one input, one action.
- Platform-specific pages can mainly change copy and color, not the core interaction.
- "How to use" and feature cards should support trust, not crowd the parser.

Risk if copied too literally:

- A single download button hides source details. Our result area should remain more transparent because users are comparing images, covers, and video assets.

Recommended stance:

- Use HelloTik as the visual baseline for calm spacing and simple copy.
- Keep our richer result area after parsing.

### SnapAny Pattern

Useful lessons:

- Broad platform support can become the public narrative.
- Desktop app and browser extension are natural future expansions for difficult sites, batch workflows, and authenticated/cookie workflows.
- It separates "web parser" from "power tools" such as batch downloads, audio/subtitle extraction, and merging.

Recommended stance:

- Short term: keep the web app focused on parsing and saving.
- Medium term: if 100+ platforms becomes real, add platform registry metadata, adapter capability flags, and maybe browser-extension planning.
- Do not add desktop-app promises before provider quality is stable.

### KuKuTool Pattern

Visible evidence from public frontend assets:

- Parse requests go through an auth/ticket step and then `/api/parse`.
- The parse request body is encrypted in the browser before being sent.
- The UI contains a `video_fullinfo` quality list and a quality-selection download button.
- The frontend contains quota/credit/no-login-limit logic.
- There is a guarded HD download flow and app/member prompts.

Interpretation:

- KuKuTool likely has a server-side provider that returns multiple video source variants.
- The ultra-HD route is not a simple public unauthenticated endpoint.
- Its "free" experience appears to be constrained by tickets, daily limits, login, reward/ad prompts, app path, or membership path.

Recommended stance:

- Do not call or depend on KuKuTool private parse APIs.
- Use it as a behavior reference only: multi-resolution candidate list, quality probing, and fallback behavior.
- Focus on implementing our own native/provider source discovery.

## Douyin Ultra-HD Evidence

### Our Baseline Samples

| Sample | Current selected source | Observed quality signal | Approx size |
| --- | --- | --- | --- |
| `https://www.douyin.com/user/self?from_tab_name=main&modal_id=7323177294577552667&showTab=record` | `api-play.amemv.com/aweme/v1/play` | 540p-style URL | 4.4 MB observed in parser output |
| `https://v.douyin.com/BHQ-oo7BqAs/` | `api-play.amemv.com/aweme/v1/play` | 540p-style URL | 5.9 MB observed in parser output |

User-provided competitor ultra-HD examples use signed `365yg.com` CDN URLs with much larger bitrate-related parameters, for example:

- `br=1371`, `bt=1371` for a smaller HD variant.
- `br=53358`, `bt=53358`, `qs=13`, `btag=80000e00010000`, `cquery=106H`, `feature_id=...` for a much larger variant.

These URLs are time-limited. Direct reuse is not stable and can return 403 after expiry.

### Working Hypotheses

1. The public web/share path often exposes convenient playback URLs, not necessarily the largest CDN object.
2. Higher variants may exist inside app-like detail responses, nested `bit_rate` entries, `video_model`, codec-specific addresses, or download-specific address fields.
3. Some variants may require signed CDN URLs generated by a server/app endpoint and may expire quickly.
4. File size is the most reliable first-pass quality signal when resolution metadata is missing or misleading.

## Differentiation Strategy

### Product Differentiation

Position the project as:

- Universal link input.
- Original-quality-first downloads.
- Transparent asset result area.
- Batch save with local progress.
- Platform adapters that can grow to many sites without changing the user workflow.

Avoid positioning as:

- A magic "always ultra-HD" tool before source discovery is stable.
- A clone of a competitor service.
- A provider aggregator that silently sends user URLs to third-party APIs.

### Technical Differentiation

Build a provider ladder:

1. Native provider: no-login, stable baseline, always enabled.
2. Native enhanced provider: deeper source enumeration, diagnostics, best-candidate selection.
3. External provider: optional, disabled by default, explicit API key and operator consent.
4. Cookie/provider path: future-only and platform-specific.

This gives the product a stronger foundation than a single hardcoded extractor.

## Next Experiments

### Experiment 1: Douyin Candidate Diagnostics

Goal: see whether our current native responses already contain hidden larger candidates.

Implementation:

- Add a diagnostic helper that dumps candidate metadata from Douyin `aweme.video`.
- Include source field, URL host, quality label, declared size, width, height, bitrate, quality type, ratio, and probe result.
- Redact volatile query values in logs/docs.

Success:

- At least one sample exposes a larger candidate before external APIs are needed.

Failure:

- Current responses only contain small `api-play.amemv.com` candidates or declared metadata without reachable larger URLs.

2026-06-07 diagnosis result for `modal_id=7618531038729648354`:

- Native mobile feed produced 29 candidates. The largest reachable candidate was 3,913,992 bytes, about 3.73 MiB.
- Native mobile share produced 7 candidates. Its apparent `4k`, `2k`, and `1080p` variants resolved to the same 2,672,155-byte source, about 2.55 MiB.
- Recursive raw `aweme.video` URL scanning did not find `365yg.com`, `qs=13`, `br=48808`, or the known ultra-HD path object.
- Query experiments on the current `api-play.amemv.com` URL with `ratio=1080p/2k/4k`, `watermark=0/1`, `improve_bitrate=0/1`, and `line=0/1/2` did not produce the 90 MiB source.
- Follow-up 8K experiments with `ratio=8k/8K/4320p/2160p/origin/source` also did not produce the 90 MiB source. The largest response stayed at about 3.73 MiB with `br=1686`, `bt=1686`, and `qs=0`.
- The confirmed 90 MiB competitor URL is a separate signed CDN object with `qs=13`, `br=48808`, `bt=48808`, and a `tos-cn-v-0015c002` path, not just a different redirect from our current native candidate.

Conclusion: this case is not a selector bug. The current native sources do not expose the ultra-HD candidate yet.

### Experiment 2: NativeEnhancedProvider

Goal: improve source enumeration without third-party APIs.

Implementation:

- Enumerate more nested Douyin fields beyond current `bit_rate` and top-level addresses.
- Probe all candidates through `QualityProbe`.
- Rank by verified size first, then resolution/bitrate/source priority.
- Record fallback reasons when the selected source remains small.

Success:

- Some known links move from 480p/540p/1080p to larger verified files.

Failure:

- No new reachable candidate is discovered after deeper enumeration.

### Experiment 3: Compare Against Known 365yg Shape

Goal: understand what metadata separates ultra-HD CDN URLs from baseline URLs.

Compare:

- Host family.
- `br` / `bt`.
- `qs`.
- `btag`.
- `cquery`.
- `feature_id`.
- Expiry-like path segments.
- Content length from HEAD or Range probe.

Success:

- We can detect when a candidate looks like a high-bitrate signed CDN object.

Failure:

- The CDN shape is only visible after an external server generates it.

### Experiment 4: External Provider Spike

Goal: decide whether a paid/free provider is worth integrating.

Rules:

- Disabled by default.
- Never send user URLs to third-party services unless configured.
- Normalize external responses to the same internal candidate shape.
- Native fallback must remain intact.

Success:

- External provider reliably returns larger validated candidates for the same samples.

Failure:

- Provider is unstable, expensive, unclear legally, or not meaningfully better than native enhanced extraction.

## Project Changes Suggested By This Research

Near-term:

- Add a Douyin diagnostics command or service. Done in `services/diagnostics.py` and `inspect_douyin_candidates`.
- Add provider diagnostics logging. Partially done through candidate diagnostics and structured runtime provider logs; user-facing/debug summary output remains pending.
- Add `NativeEnhancedProvider` before adding real external API calls. Done as disabled-by-default `DouyinNativeEnhancedProvider`.
- Keep current result UI mostly unchanged except for download progress polish.

Medium-term:

- Add adapter capability metadata: images, video, live photo, batch, auth required, merge required, ultra-HD supported.
- Add platform registry data for future 100+ platform support.
- Add per-platform SEO pages only when the adapter is real or planned soon.

Long-term:

- Consider browser extension or desktop companion only after the provider layer is stable.
- Consider cookie/session workflows only with explicit user consent and storage design.

## Decision

Native diagnostics and 8K query experiments have now shown that the known 90 MiB source is not exposed by the current no-login native routes. The provider sequence is:

1. Keep native diagnostics and `DouyinNativeEnhancedProvider` as the safe local baseline.
2. Add a disabled-by-default TikHub provider for Douyin highest-quality play URLs.
3. Validate returned external URLs with `QualityProbe` before selecting them.

## Current Checkpoint

The project now has the TikHub provider path and cost controls, but competitor-level ultra-HD speed has not been matched. Manual tests still show some ultra-HD parses taking more than 10 seconds, while competitor pages can appear to return ultra-HD choices in roughly 3 seconds. The next differentiating task is not another UI redesign; it is provider timing observability and slow-segment diagnosis.

Recommended immediate experiment:

1. Inspect and, if needed, extend structured server-side timing around TikHub API, candidate probing, native fast-path fallback, and final selection.
2. Re-test the known slow links with the same `.env` and settings switch.
3. Decide from measured data whether to reduce probe count, cache provider metadata more aggressively, add a "quick ultra" mode, or research a second provider.
4. Fall back to native providers when TikHub is not configured, fails, or returns unvalidated media.

This keeps the product differentiated by control, transparency, and stable fallback while allowing operator-approved third-party ultra-HD experiments.
