# Provider Runtime Gates

AnimeImages separates Provider configuration into five layers. This keeps deployment capability, product rollout, platform exposure, and safety limits from becoming one overloaded switch.

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

Production checks still validate dependencies and admin health exposes readiness. Missing optional Provider dependencies are warnings by default so the site can start with admin runtime modes still disabled. Set `PROVIDER_STRICT_CAPABILITY_CHECKS=True` to turn those warnings into release-blocking errors.

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
douyin_ultra_rollout_percent=0
youtube_merge_rollout_percent=0
third_party_rollout_percent=0
third_party_allowed_public_platforms=
third_party_allowed_public_hosts=
third_party_blocked_platforms=
third_party_blocked_hosts=
```

After deployment, routine rollout should happen in `/admin/provider-settings/` instead of by editing `.env`:

1. Deploy with capability defaults prepared and dependencies installed where possible.
2. Run health checks and staff smoke tests.
3. Move runtime mode from `disabled` to `staff`.
4. Move to `public_gray` and raise that Provider's rollout percentage gradually.
5. Move to `public` only after latency, logs, and download recovery look stable.

Provider runtime changes are written to `ProviderAdminAuditLog` and can be reviewed at `/admin/provider-audit-logs/`. Daily rollout should be checked together with `/admin/ops-dashboard/`, parse logs, and download logs; see `docs/admin-observability.md`.

Emergency shutdown should also use admin `disabled` first. Change `.env` only when the server must completely remove the capability or when dependencies are missing.

## Platform Gate

The generic third-party Provider has an additional platform gate. This is required because yt-dlp and gallery-dl support many sites, including sites that should not be public by default.

For anonymous visitors, `third_party_mode=public_gray` or `third_party_mode=public` is not enough. A URL must also match one of:

- `third_party_allowed_public_platforms`
- `third_party_allowed_public_hosts`

If both allowlists are empty, the generic third-party Provider is not public for visitors.

Blocklists always win:

- `third_party_blocked_platforms`
- `third_party_blocked_hosts`
- built-in blocked public host suffixes

Host matching uses strict suffix boundaries. For example, `youtube.com` allows `www.youtube.com` and `m.youtube.com`, but not `youtube.com.evil.com`.

Each built-in third-party platform has a risk level:

- `safe`
- `normal`
- `sensitive`
- `adult`
- `login_required`
- `unknown`

`public_default` is only an operator hint. It never automatically exposes a site to visitors. `adult` and `login_required` platforms are not available to visitors by default, and staff users cannot bypass those hard blocks unless an explicit internal-only setting is added and enabled. The product does not upload cookies for public users, does not bypass login or age checks, and does not provide a public parser for adult or login-required sources.

Staff users can smoke-test normal or sensitive platforms when the Provider runtime mode allows staff access, unless a platform or host is explicitly blocked.

## Safety Limits

Safety limits are still environment or deployment configuration. They protect server cost, bandwidth, queue pressure, and upstream risk:

```text
THIRD_PARTY_RATE_LIMIT_PER_MINUTE=10
THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=30
THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=3.0
YOUTUBE_FRONTEND_TIMEOUT_SECONDS=8.0
THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES=8388608
YOUTUBE_MERGE_MAX_FILE_SIZE_BYTES=1073741824
KUKUTOOL_GLOBAL_REQUEST_LIMIT_PER_MINUTE=30
```

These values should be stricter in production than local development. They can later move into database-backed operations settings, but they should remain separate from the runtime exposure mode.

## Rollout Gate

Each Provider has an independent rollout percentage in the admin runtime setting. The percentage only applies when that Provider mode is `public_gray`.

- `0`: no anonymous visitor enters the gray path.
- `100`: all anonymous visitors enter the gray path.
- Staff users are always allowed in `public_gray`.
- A deterministic hash of user id or request identity keeps gray rollout stable between requests.
- Provider buckets are independent, so YouTube merge rollout does not affect third-party or Douyin ultra-HD rollout.

Recommended public rollout:

1. Deploy capability and dependencies.
2. Keep admin mode `disabled`.
3. Run staff smoke tests.
4. Move to `public_gray` at 1%.
5. Move to `public_gray` at 5%.
6. Move to `public`.
7. If anything looks wrong, switch the Provider back to `disabled` in admin immediately.

## Provider Lab Gate

Provider Lab uses a separate Staff-only gate. It does not participate in
`ProviderRuntimeSetting`, `public_gray`, anonymous rollout, or the generic
platform allowlists described above.

The base Lab gate is:

```text
UPSTREAM_PROVIDER_LAB_ENABLED=True
active authenticated Staff user
valid CSRF token
Lab request rate limit
per-Provider single-flight
```

The whole-repository videodl Worker has an additional capability gate:

```text
VIDEODL_PROVIDER_LAB_ENABLED=True
```

When either required flag is `False`, the corresponding Lab surface fails
closed. The Lab cannot be exposed to anonymous users through
`/admin/provider-settings/`, and it has no `disabled / staff / public_gray /
public` release sequence. The browser sends one POST per selected Provider,
with at most three Provider requests in flight; Django does not automatically
fan out to every Lab Provider.

ParseHub XHS Runtime and the ParseHub Provider Lab Worker are different
capabilities. The former supplies candidates inside the fixed XHS image route;
the latter compares a whole-repository parser in an isolated Staff Lab. Their
flags and Runtime paths are not interchangeable.

videodl remains subject to PolyForm Noncommercial 1.0.0 and the existing
approved-use boundary. Current code can consume controlled execution in shared
parsing and explicitly saved local host routes, including only the reviewed
Common backends described in the Lab guide. This does not expand anonymous
admission or grant production/commercial permission. Source access, runtime
policy and host/backend-bound future-sharing authorization are separate from
license approval; uses beyond the recorded authorization remain to be verified.

Provider Lab operations, exact repository pins, bootstrap commands, result
governance, and cleanup are documented in [Provider Lab](provider-lab.md).

## KuKuTool Strategy

Douyin KuKuTool ultra-HD is riskier than the regular yt-dlp/gallery-dl capability because it depends on a third-party upstream, dynamic profile behavior, and anti-abuse conditions.

The default production posture is operational: keep KuKuTool capability prepared when dependencies exist, but keep `douyin_ultra_mode=disabled` in admin until staff smoke is complete. If a server must fully remove this capability, set `KUKUTOOL_PROVIDER_ENABLED=False`. That switch turns off KuKuTool only; Douyin ultra-HD stays available through the other services in the configured order, such as TianTian. `ULTRA_HD_PROVIDER_ENABLED=False` removes all Douyin and XHS ultra-HD.

Admin runtime mode remains the daily switch for staff/public access. Missing Node/sidecar dependencies should appear as health readiness failures, not as routine public access switches.
