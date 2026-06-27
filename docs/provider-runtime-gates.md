# Provider Runtime Gates

AnimeImages separates Provider configuration into four layers. This keeps deployment capability, product rollout, and safety limits from becoming one overloaded switch.

## Capability Gate

The `.env` Provider `ENABLED` values describe whether this server is allowed and prepared to run a capability.

Examples:

```text
THIRD_PARTY_PROVIDER_ENABLED=True
YTDLP_PROVIDER_ENABLED=True
GALLERYDL_PROVIDER_ENABLED=True
YOUTUBE_MERGE_PROVIDER_ENABLED=True
YTDLP_MERGE_PROVIDER_ENABLED=True
```

These values do not mean anonymous users can use the feature. They mean the deployment has the intended runtime, dependencies, and safety checks available. If a capability gate is `False`, the Provider is unavailable even when the admin runtime setting is `public`.

Production checks still validate dependencies. For example, if `YTDLP_PROVIDER_ENABLED=True`, production must provide a working `YTDLP_BINARY`; if `YOUTUBE_MERGE_PROVIDER_ENABLED=True`, production must provide yt-dlp and ffmpeg commands.

`THIRD_PARTY_PUBLIC_ACCESS_ENABLED` and `YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED` remain compatibility flags in older diagnostics, but daily access control belongs in the admin runtime setting.

## Runtime Gate

`ProviderRuntimeSetting` in the admin console decides the current product exposure:

- `disabled`: no user can enter that Provider path.
- `staff`: staff users can smoke test; visitors cannot.
- `public_gray`: staff users can use it, and visitors enter according to the rollout percentage.
- `public`: all visitors can use it.

The initial database defaults are conservative:

```text
douyin_ultra_mode=disabled
youtube_merge_mode=disabled
third_party_mode=disabled
rollout_percent=0
```

After deployment, routine rollout should happen in `/admin/provider-settings/` instead of by editing `.env`:

1. Deploy with capability and dependencies configured.
2. Run health checks and staff smoke tests.
3. Move runtime mode from `disabled` to `staff`.
4. Move to `public_gray` and raise `rollout_percent` gradually.
5. Move to `public` only after latency, logs, and download recovery look stable.

Emergency shutdown should also use admin `disabled` first. Change `.env` only when the server must completely remove the capability or when dependencies are missing.

## Safety Limits

Safety limits are still environment or deployment configuration. They protect server cost, bandwidth, queue pressure, and upstream risk:

```text
THIRD_PARTY_RATE_LIMIT_PER_MINUTE=2
THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=30
THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=3.0
YOUTUBE_FRONTEND_TIMEOUT_SECONDS=8.0
THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES=8388608
YOUTUBE_MERGE_MAX_FILE_SIZE_BYTES=524288000
KUKUTOOL_GLOBAL_REQUEST_LIMIT_PER_MINUTE=30
```

These values should be stricter in production than local development. They can later move into database-backed operations settings, but they should remain separate from the runtime exposure mode.

## Rollout Gate

`rollout_percent` belongs to the admin runtime setting and only applies when a Provider mode is `public_gray`.

- `0`: no anonymous visitor enters the gray path.
- `100`: all anonymous visitors enter the gray path.
- Staff users are always allowed in `public_gray`.
- A deterministic hash of user id or request identity keeps gray rollout stable between requests.

## KuKuTool Strategy

Douyin KuKuTool ultra-HD is riskier than the regular yt-dlp/gallery-dl capability because it depends on a third-party upstream, dynamic profile behavior, and anti-abuse conditions.

Two production strategies are acceptable:

- Conservative: keep `KUKUTOOL_PROVIDER_ENABLED=False` until staging proves the sidecar/runtime, latency, and download recovery.
- Operational: set `KUKUTOOL_PROVIDER_ENABLED=True` after dependencies are installed, but keep `douyin_ultra_mode=disabled` in admin until staff smoke is complete.

In both cases, admin runtime mode remains the daily switch for staff/public access.
