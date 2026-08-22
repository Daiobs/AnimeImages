# First Rich Media Consumer Plan

## Decision

`FIRST_CONSUMER = BILIBILI_DYNAMIC_LIVEPHOTO`

This remains a plan, not an implementation or rollout decision. The offline
contract is complete and the independent runtime contract is implemented only
in Draft. No platform emits it yet.

## Why this consumer

The current AnimeImages Bilibili adapter is deliberately narrow: it recognizes
baseline video pages/short links, reads public view/play data, and returns video
assets with cover metadata. It has no Dynamic/Opus post model.

The pinned ParseHub research baseline reports static implementation evidence for
Bilibili Dynamic image and Live Photo-like media. That makes the platform a
useful first proof of the domain contract without replacing the accepted video
adapter. The choice also follows the existing research priority: external-tool
lifecycle hardening is complete, the domain contract is the next offline gate,
and Dynamic support remains separately gated.

No live endpoint, Cookie, source post, media location or Provider payload was
used for this decision.

## Required first-consumer behavior

The future Bilibili Dynamic task must prove, with fictional fixtures first:

1. stable source identity for the requested Dynamic/Opus item;
2. ordered image/video extraction before type filtering;
3. exact still/motion pairing for Live Photo items;
4. no recommendation or adjacent-post media admission;
5. admitted page and media host boundaries after every redirect;
6. one request deadline propagated through every request;
7. no credential, Cookie or raw payload in results/logs;
8. each physical member receives its own current AnimeImages asset identity;
9. relationship validation happens before view construction; and
10. download authorization remains per physical member.

## Proposed mapping

| Dynamic result | v1 post type | Assets | Groups / blocks |
| --- | --- | --- | --- |
| One still image | `image` | One image | None |
| Ordered still collection | `gallery` | Images in source order | None |
| One still/motion pair | `live_photo` | Image then video in source order | One exact pair |
| Images plus video/pairs | `multimedia` | One mixed ordered tuple | Live Photo groups annotate members |
| Dynamic text plus media | Deferred from first runtime slice | Same canonical assets | Requires later rich-text presentation review |

The first slice should prefer image/gallery/Live Photo semantics and defer rich
text rendering. It may retain title/author metadata under current safe display
contracts without adding arbitrary text blocks.

## URL and registry plan

The future adapter work must add explicit Bilibili Dynamic/Opus recognition to
the existing registry. It must not broaden the current generic third-party rule
or route unknown Bilibili pages into the video adapter. URL patterns and
redirect behavior require a separate static/live security review.

## Presentation plan

Before public UI work:

- add a shared ordered result projection used by Single and Batch;
- render Live Photo as one semantic unit while retaining two selectable assets;
- use escaped fixed UI labels only;
- do not automatically add cover metadata to canonical ordering; the future
  Provider builder must explicitly identify canonical content assets;
- do not implicitly select/download the motion member; and
- retain all existing Public/Staff raw-link boundaries.

Rich text remains out of the first presentation slice.

## Download plan

No relationship-level download endpoint or token is allowed. A paired download
action may assemble an explicit list of two current download items, but both
must pass existing validation independently. Browser direct, photo archive and
server-generated paths keep their current resource-type contracts.

`RICH_MEDIA_RELATION_IS_NOT_DOWNLOAD_AUTHORITY`

## Future PR split

### PR A - Runtime contract (implemented in Draft)

- Add an optional AnimeImages-owned rich-media value and validator.
- Preserve `ParseResult.assets`, legacy views and serialization.
- Add no platform parsing.

### PR B - Bilibili Dynamic research adapter

- Add static fictional payload fixtures and source-identity tests.
- Map ordered physical assets and Live Photo groups.
- Keep the capability inaccessible to public requests.

### PR C - Staff-only integration

- Add explicit registry routing and runtime capability gate.
- Run bounded staff-only live smoke with sanitized evidence.
- Confirm host, deadline, credential and recommendation boundaries.

### PR D - Presentation and download

- Add shared ordered Single/Batch projection.
- Add paired UI with independent asset admission.
- Regress Browser/ZIP/generated-output contracts.

### PR E - Production acceptance and public rollout

- Separate exact-SHA deployment task.
- Complete staff then public access matrix and browser acceptance.
- Roll back on identity, host, authorization or media-byte regressions.

No later PR may be folded into PR A to accelerate platform rollout.

## Exit criteria for the next task

The Bilibili Dynamic research adapter task may start only after the runtime
contract Draft is reviewed and merged. It must remain offline unless its own
instructions explicitly authorize a bounded staff-only live phase. Production
remains out of scope until PR E.
