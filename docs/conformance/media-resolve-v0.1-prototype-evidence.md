# Media Resolve v0.1 Project-local Prototype Evidence

> **Status: `ACCEPTED / PROJECT-LOCAL PROTOTYPE EVIDENCE V1`**
>
> **Human Disposition: `ACCEPT`**
>
> **Realization: `EXTRACT_IN_PLACE / PROJECT_LOCAL PROTOTYPE`**
>
> **Runtime integration: `NONE`**
>
> **Current consumer claim: `NONE`**
>
> **Lifecycle effect: `NONE`; `media-resolve` remains `REFERENCE`**

This is Human-accepted bounded implementation and same-implementation Core
Conformance evidence. It is not a canonical-implementation, Adapter, Delivery,
compatibility, current-consumer, production-feature, or lifecycle-promotion
claim.

## Human acceptance

Human disposition is `ACCEPT`. The accepted facts are:

- a Media Resolve v0.1 Prototype implementation exists in AnimeImages;
- the exercised realization is `EXTRACT_IN_PLACE / PROJECT_LOCAL PROTOTYPE`;
- Core Conformance is `12/12 PASS`, and the same implementation produced both
  real-domain mappings and all twelve vectors;
- production integration, current consumer, canonical implementation claim,
  lifecycle effect, and Foundry Schema effect are all `NONE`.

Acceptance does not authorize runtime wiring, Provider or Delivery integration,
a Douyin Adapter, Workbench integration, a Registry current-consumer claim,
lifecycle promotion, Schema v0.2, or a canonical-implementation declaration.
`media-resolve` remains `REFERENCE`.

## 1. Authorities and review snapshots

| Authority | Recorded value |
| --- | --- |
| AnimeImages repository | `Daiobs/AnimeImages` (private) |
| Remote default branch | `main` |
| Known observed SHA from task | `176e4e3b0975ee66bf7788e1b826c06b40e38502` |
| Implementation Base SHA | `cf19ef32ac2d2730c5ed4511f516eedc557f84c4` |
| Initial locally tested Prototype commit | `01a0df5ff0983b160360c229b38ae4991a2de12a` |
| Initial remote implementation commit | `41c1da71595cb4f363f8ead6e8f0188054ecba40` |
| Initial implementation tree | `b04fc08730b9ec6068140c259906a46dc789abfb` |
| Verified local final snapshot commit | `b97e471c39fb2eb822b0c3b560143217b9e9fdb2` |
| Verified local final snapshot tree | `aa2f1d74e7670d2abba08ce9f29240a0ab17cc28` |
| Human-reviewed remote snapshot Head | `092bf1f79d6d3d0985e046f808622e22025f0986` |
| Human-reviewed remote snapshot tree | `aa2f1d74e7670d2abba08ce9f29240a0ab17cc28` |
| Human-reviewed PR Base | `3b396f75968ad0a0f92cb089f9705dd93d5e4e8e` |
| Final reviewed PR CI | `32734911706` — `SUCCESS` |
| Final reviewed push CI | `32734906064` — `SUCCESS` |
| Core implementation blob | `427f6e50122464de73e22622de7447802c5050fd` |
| Projection implementation blob | `ce7381cc41008520a1881d0bf74f6a963ebc3b22` |
| Conformance test blob | `cbd1806f2ca8ef26e8ced1b145edcaceb4d92f9c` |
| Human-reviewed PR snapshot | `#69` — `https://github.com/Daiobs/AnimeImages/pull/69` |
| Historical Sample B SHA | `2373bdf996ccd0f2596eb081a5ea781b7c268a6a` |
| Foundry Contract repository | `Brilliant666/cosflow-product-assets` |
| Contract acceptance SHA | `b0e94237b182d41377401c74f79e60aa07765cbe` |
| RFC path | `catalog/media-resolve/media-resolve/rfc/RFC-0001-MEDIA-RESOLVE-V0.1.md` |
| RFC blob SHA | `a98cab514974c494a34930403dd8bd1ee731acb2` |
| Conformance Plan path | `catalog/media-resolve/media-resolve/rfc/MEDIA_RESOLVE_CONFORMANCE_PLAN.md` |
| Conformance Plan blob SHA | `215ec7407ba074880a7eaddc229abd93a0ac87de` |
| Contract target | `v0.1`; exactly 6 Core objects and 12 Core vectors |
| Model Review acceptance SHA | `a2757f458def053566217d746ef5db561eda6242` |
| Model Review authority path | `reviews/foundry-model-v1/FIRST_EXECUTABLE_ASSET_DECISION.md` |
| Model Review blob SHA | `4da3fa45189c13d62f022ebd8bf79d760348b14b` |
| Accepted outcome | `SELECT_MEDIA_RESOLVE_CONFORMANCE_WITH_FIRST_IMPLEMENTATION` |

The RFC, Conformance Plan, and Model Review were read from their fixed commits.
Their expected blob SHAs, accepted statuses, object/vector budgets, and selected
outcome matched exactly. `CONTRACT_AUTHORITY_DRIFT = NO`.

GitHub Git Database API serialized the corresponding remote commits
independently after HTTPS Git transport was unavailable. Therefore local commit
`01a0df5ff0983b160360c229b38ae4991a2de12a` and remote commit
`41c1da71595cb4f363f8ead6e8f0188054ecba40` differ while sharing initial tree
`b04fc08730b9ec6068140c259906a46dc789abfb`; likewise verified local final
commit `b97e471c39fb2eb822b0c3b560143217b9e9fdb2` and Human-reviewed remote Head
`092bf1f79d6d3d0985e046f808622e22025f0986` differ while sharing complete
reviewed tree `aa2f1d74e7670d2abba08ce9f29240a0ab17cc28`.

Tree `b04fc08730b9ec6068140c259906a46dc789abfb` is the initial executable
Prototype tree. Tree `aa2f1d74e7670d2abba08ce9f29240a0ab17cc28`
is the complete Human-reviewed evidence snapshot tree. The three code/test blob
SHAs in the table stayed unchanged, and the only change from remote commit
`41c1da71595cb4f363f8ead6e8f0188054ecba40` to
`092bf1f79d6d3d0985e046f808622e22025f0986` was this Evidence document. No Core,
projection, or test behavior changed during evidence finalization. The later
acceptance-metadata Head and Squash Merge SHA are intentionally recorded in Git
history and the acceptance task report rather than recursively in this file.

## 2. Product drift and eligibility

The remote `main` observed at task start had advanced from the task's known SHA
to `cf19ef32ac2d2730c5ed4511f516eedc557f84c4`. It did not move again between
preflight and branch/worktree creation. Relative to historical Sample B SHA
`2373bdf996ccd0f2596eb081a5ea781b7c268a6a`, the relevant commits added:

- reviewed Douyin terminal redirect evidence and telemetry assertions;
- an unwired Source Lifecycle contract;
- an unwired XHS rich-media normalizer;
- unwired Bilibili Dynamic/LivePhoto research and normalizer;
- repository-safe XHS LivePhoto payload evidence.

After branch/worktree creation, remote `main` advanced once more to
`3b396f75968ad0a0f92cb089f9705dd93d5e4e8e` (`Implement dormant XHS LivePhoto
relationship mapper v1 (#68)`). Its four-path delta was limited to
`CHANGELOG.md`, one research document, one dormant relationship-mapper module,
and its test. It touched none of this task's four target paths or the
`ParseResult`/`MediaAsset` seam. In accordance with the task's fixed-Base rule,
the implementation retained Base `cf19ef32ac2d2730c5ed4511f516eedc557f84c4`
and was not rebased. `EXECUTION_TIME_PRODUCT_DRIFT_CONFLICT = NO`.

The targeted diff proved that `xhs_download/services/assets.py`,
`candidates.py`, `registry.py`, and `rich_media.py` did not change across that
range. The newer XHS and Bilibili modules explicitly produce dormant specs,
not `MediaAsset`, and their tests prove that current adapters and the registry
do not import them. The current latest-tree `ParseResult` and `MediaAsset`
therefore remain the shared media/result domain, with both one-asset and
multi-asset construction paths.

Eligibility gates A-I were all `YES` before implementation:

- the Contract and Model Review authorities were exact;
- both mandatory real-domain paths were available;
- a new provider-neutral adjacent module location was free;
- neither path needed Provider, network, credential, database, Redis, route,
  view, Admin, registry, download, or deployment behavior;
- only new files were required;
- production imports remained zero;
- deletion of the four files is a complete rollback.

`ELIGIBILITY = ELIGIBLE`.

## 3. Implementation

Exactly four new paths comprise the Prototype:

1. `xhs_download/services/media_resolve_v0_1.py`
2. `xhs_download/services/media_resolve_projection_v0_1.py`
3. `xhs_download/tests_media_resolve_v0_1.py`
4. `docs/conformance/media-resolve-v0.1-prototype-evidence.md`

Existing files modified: `0`. Deleted files: `0`.

The six and only six Core dataclasses are:

1. `ResolveRequest`
2. `ResolveResult`
3. `SourceIdentity`
4. `MediaItem`
5. `CandidateProjection`
6. `MediaCandidate`

Supporting enums, one internal validation exception, and pure validation
functions add no consumer domain object. The Core is standard-library-only,
transport-neutral, frozen, and independent of Django settings. It implements
the accepted request budget, mutually exclusive result groups, multi-media
Core, explicit item/candidate completeness, selection ownership, ID scope and
alias rejection, four truth states, four failures with actionability, strict
fact shapes, deterministic order, redaction, and `NO_CORE_HANDOFF`.

`project_parse_result` is the single first-party projection path. It accepts a
current-tree `ParseResult`, a `ResolveRequest`, an already Adapter-verified
`SourceIdentity`, a caller-supplied safe `resultId`, and explicit completeness
and selection semantics. It neither discovers nor verifies identity from a
locator. Result-local positional IDs are deterministic and do not depend on a
locator, Header, Provider field, product asset ID, or raw metadata.

There is no runtime wiring, current consumer, shared Package, Service, API,
SDK, canonical implementation, Provider, Delivery, or product behavior change.

## 4. Mandatory real-domain mappings

### 4.1 Single media — MR-CV-01

The test constructs the latest-tree real domain types:

```text
ParseResult
  -> one MediaAsset(media_type=video, size_bytes=4096,
                    size_is_estimate=False)
  -> project_parse_result
  -> RESOLVED / one VIDEO MediaItem / one redacted MediaCandidate
```

It proves verified `SourceIdentity` input, `resultId=result-single`,
`mediaItemId=item-0001`, `candidateId=candidate-0001-0001`, explicit item and
candidate completeness, resolver-owned selection, deterministic repeat output,
safe same-result ID reuse, conflicting-result alias rejection, and
`ASSERTED bytes=4096`. The synthetic product quality label does not create a
dimension or measured-quality fact.

### 4.2 Multiple media — MR-CV-11

The test constructs one latest-tree `ParseResult` containing two latest-tree
`MediaAsset` instances in source order: one image and one video. Projection
returns two deterministically ordered items, each with a non-empty projection,
unique item IDs, and result-wide unique candidate IDs. The image estimate maps
to `ASSERTED estimatedBytes`; the current upstream video total maps to
`ASSERTED bytes`.

The vector also proves ordinary known omission becomes `BOUNDED` plus
`MEDIA_ITEMS_OMITTED`, selected IDs resolve within their own projection,
duplicate item/candidate IDs fail, and a security failure has no per-item
success downgrade. No `RichMediaContractV1` group, layout, relation, or content
block is invented.

### 4.3 Explicit exclusions

The projection does not copy these current `MediaAsset` fields into Core output
or derive an ID from them:

- `url`, `preview_url`, `cover_url`, `cover_preview_url`;
- `headers`;
- `provider`, `provider_resource_id`, `provider_cache_key`,
  `provider_manifest_token`;
- `media_host_suffixes`;
- `quality_label`;
- `ParseResult.metadata` or `rich_media` relations.

Synthetic redaction markers placed in those fields were absent from Core
`repr` output. The request reference is also `repr=False` and is never copied
to `ResolveResult`.

### 4.4 Accepted binding boundary

`MediaAsset.url` is inspected only as the current AnimeImages-domain signal
that an asset has a representation. It is not a validated locator, source
identity evidence, or Delivery-eligibility proof; it is not copied into Core
and is not used for ID derivation.

`sourceIdentity` (`SourceIdentity`) is trusted as an Adapter-verified input.
`mediaItemsCompleteness`, `candidateCompleteness`, and `selectionOwner` are
binding-supplied semantic facts. This unwired Prototype does not prove that a
production binding supplies those facts correctly; that proof belongs to a
future Adapter/runtime-integration task.

## 5. Core Conformance

The authoritative class-only command executed exactly 12 tests and reported:

```text
Ran 12 tests
OK
12 passed / 0 failed / 0 skipped
```

| Vector | Test name | Result | Evidence source | Domain basis and note |
| --- | --- | --- | --- | --- |
| MR-CV-01 | `test_mr_cv_01_supported_single_media_source` | PASS | same projection | Real latest-tree single `ParseResult/MediaAsset`; deterministic BEST_AVAILABLE application, redaction, IDs, facts, and result aliasing |
| MR-CV-02 | `test_mr_cv_02_unsupported_source` | PASS | same Core | Synthetic offline failure; complete safe actionability and no resolution group |
| MR-CV-03 | `test_mr_cv_03_no_usable_media` | PASS | same projection/Core | Synthetic current domain; type mismatch and `UNKNOWN` do not satisfy VIDEO; no empty success |
| MR-CV-04 | `test_mr_cv_04_coarse_retryable_resolution_failure` | PASS | same Core | Synthetic offline retryable failure with no Provider/timeout detail |
| MR-CV-05 | `test_mr_cv_05_unknown_optional_physical_facts` | PASS | same Core | Synthetic inline facts preserve five explicit UNKNOWN states |
| MR-CV-06 | `test_mr_cv_06_assertion_versus_measurement` | PASS | same Core | Synthetic ASSERTED/OBSERVED/MEASURED distinctions; estimate cannot be MEASURED; label creates no fact |
| MR-CV-07 | `test_mr_cv_07_service_style_selected_only_projection` | PASS | same Core | Synthetic URL-free selected-only service projection |
| MR-CV-08 | `test_mr_cv_08_selected_plus_bounded_alternatives` | PASS | same Core | Synthetic bounded alternatives, matching selected ID, deterministic canonical order; count implies nothing |
| MR-CV-09 | `test_mr_cv_09_genuinely_exhaustive_projection` | PASS | same Core | Synthetic known-set proof passes complete and rejects omission |
| MR-CV-10 | `test_mr_cv_10_consumer_owned_selection` | PASS | same Core | Synthetic Consumer ownership omits selected ID, rejects hidden selection, preserves immutable later choice boundary |
| MR-CV-11 | `test_mr_cv_11_multi_asset_result` | PASS | same projection | Real latest-tree multi `ParseResult/MediaAsset`; order, completeness, facts, IDs, omission, security, redaction, no relations |
| MR-CV-12 | `test_mr_cv_12_security_rejection_and_core_surface_closure` | PASS | same Core | Synthetic fail-closed security result, six-object closure, typed ID separation, prohibited-field closure |

The complete new module ran `22/22` tests and Django reported
`Skipping setup of unused database(s): default`. The ten additional tests are
class-labeled `NON_CONFORMANCE_SUPPORTING_TEST`; they cover result aliasing,
duplicate item/candidate IDs, hidden Consumer selection, locator-shaped IDs and
ID independence from locator/Provider fields, production import isolation,
static import closure, dynamic no-network/no-Django/no-Redis/no-registry import,
no persistent writes, and the deletion-only path inventory. They are not a
thirteenth vector.

## 6. Isolation and no-mutation evidence

| Boundary | Result | Evidence |
| --- | --- | --- |
| Existing production imports | `0` | repository scan excluding the two Prototype modules, test, and this report |
| Routes/views/Admin/AppConfig/signals/commands/tasks wired | `0` | fixed production-boundary scan and repository grep |
| `PlatformRegistry` / Provider registry registration | `0` | dynamic isolated import confirms registry module remains unloaded |
| Django settings imports | `0` | AST scan plus isolated process that rejects every `django` import |
| Database access | `0` | `unittest.TestCase`, blocked Django/psycopg imports, and Django skipped the database |
| Redis access | `0` | isolated process rejects `redis` import |
| Network access | `0` | isolated import and projection execute with socket constructors denied |
| Persistent mutation | `0` | projection executes while file-write APIs are denied |
| Provider calls | `0` | no Provider import or call surface |
| Delivery integration | `0` | no locator output, handoff, download, stream, storage, or Delivery import |

All fixtures are independently authored, synthetic, deterministic, offline,
and contain no real user data, production identifier, credential, Cookie,
bearer material, signed media locator, real media URL, or raw Provider response.
No external baseline code, class, schema, fixture, or test was copied or
translated.

## 7. Existing-product baseline and regression result

The clean Base and modified worktree used the same task-owned dependency
environment. The local host offers Python 3.10/Windows rather than the
repository-pinned Python 3.14/Linux CI environment, so platform-only failures
are reported rather than concealed.

| Check | Before at Base | After Prototype | Interpretation |
| --- | --- | --- | --- |
| Latest official main CI | success, run `32728285058`, exact Base SHA | Human-reviewed snapshot: PR run `32734911706` success; push run `32734906064` success | Base and complete reviewed snapshot are green; acceptance-head CI is recorded by GitHub and the acceptance task report |
| `manage.py check` | PASS; 4 missing optional local-tool warnings | PASS; identical warnings | no regression |
| `makemigrations --check --dry-run` | PASS / no changes | PASS / no changes | no schema change |
| Relevant RichMedia/XHS/Bilibili/Source Lifecycle/telemetry tests | 166/166 PASS | 166/166 PASS | no regression |
| New Core-vector class | not present | 12/12 PASS, 0 failed, 0 skipped | conformance PASS |
| Complete new module | not present | 22/22 PASS, database skipped | supporting gates PASS |
| Full local Python suite | 1422 run; 16 failures, 21 errors, 11 skipped | 1444 run; same 16 failures, 21 errors, 11 skipped | 22 new passes; unchanged Windows/encoding/subprocess/timing failure signature |
| Node syntax and workflow test selection | official Base CI success | 141/141 PASS locally | no frontend changes |
| Nginx repository drift | official Base CI success | `NGINX_CONFIG_DRIFT=0` | no infrastructure changes |

### 7.1 Human-acceptance finalization validation

The acceptance task reran these gates against the unchanged implementation and
test blobs before creating the acceptance-metadata commit:

| Acceptance gate | Result | Recorded evidence |
| --- | --- | --- |
| Code/test blob stability | PASS | Core `427f6e50122464de73e22622de7447802c5050fd`; projection `ce7381cc41008520a1881d0bf74f6a963ebc3b22`; tests `cbd1806f2ca8ef26e8ced1b145edcaceb4d92f9c` |
| Starting-snapshot delta | PASS | only `docs/conformance/media-resolve-v0.1-prototype-evidence.md` changed from the clean reviewed local snapshot |
| MR-CV-01 through MR-CV-12 | PASS | 12 passed, 0 failed, 0 skipped |
| Complete new test module | PASS | 22/22, including all ten `NON_CONFORMANCE_SUPPORTING_TEST` cases |
| Relevant existing test selection | PASS | 245/245 across RichMedia, XHS, Bilibili, Source Lifecycle, LivePhoto evidence, and browser telemetry |
| `manage.py check` | PASS | only the same four unavailable optional local-tool warnings |
| `makemigrations --check --dry-run` | PASS | no changes detected |
| Import boundary / no network / no persistent mutation | PASS | rerun by the supporting tests in the complete 22-test module |
| `git diff --check` | PASS | no whitespace error |

The unchanged local Python failures are outside the modified paths and consist
of Windows default-encoding reads, Windows subprocess/path semantics, and
pre-existing timing/data-order assertions. The local Nginx unit selection has
one Windows effective-dump formatting error; the exact Base's Linux CI passes.
No failure names, failure/error counts, or related-test results regressed after
the Prototype. GitHub's Linux/Python 3.14 CI is the authoritative final
cross-platform gate and is recorded per reviewed or acceptance Head outside any
recursive Head-SHA claim in this document.

## 8. Removal boundary and ownership

Complete rollback is deletion of the four paths in Section 3. It requires:

- no migration or data rollback;
- no settings or credential restoration;
- no route, registry, runtime, Provider, or Delivery rollback;
- no deployment rollback;
- no consumer migration or compatibility migration;
- no external cleanup.

The AnimeImages product owner owns the project-local Prototype code, P0 guard,
maintenance, and deletion decision. The Media Resolve Contract steward in
`Brilliant666/cosflow-product-assets` owns semantic interpretation and future
Contract/vector revision. This task creates no shared Service, availability,
incident, Provider, or cross-product upgrade owner.

## 9. Foundry v0 model feedback

The existing v0 Card can record this bounded Prototype and conformance result
honestly through free-text implementation state, candidate implementations,
evidence, and associated documents. The first real `NONE -> PROTOTYPE`
transition nevertheless exposes these queryability costs for the separately
authorized Post-implementation Model Evidence Review:

| Fact | Assessment | Evidence from this execution |
| --- | --- | --- |
| `implementation_maturity` | `STRUCTURED_FIELD_CANDIDATE` | The first governed `PROTOTYPE` exists while v0 `implementation.state` is unconstrained prose and lifecycle correctly stays `REFERENCE`. |
| `realization_mode` | `STRUCTURED_FIELD_CANDIDATE` | `EXTRACT_IN_PLACE` changed from a hypothesis to an exercised, selected realization; a bare scalar still needs decision-strength/history semantics. |
| `canonical_implementation` | `MINOR_FRICTION` | Exact repository/path/revision is recordable in evidence, and the correct value is still “not canonical”; that distinction is prose rather than typed. |
| `canonical_artifacts` | `STRUCTURED_FIELD_CANDIDATE` | Contract commit, two governed blobs, Model Review authority, implementation commit, test module, and evidence report now have distinct roles and revisions. |
| role-scoped ownership | `STRUCTURED_FIELD_CANDIDATE` | Product-code/removal ownership and Contract semantic stewardship are both real and must not collapse into one flat owner. |
| compatibility/conformance record | `STRUCTURED_FIELD_CANDIDATE` | Same-implementation Core conformance is 12/12, while consumer compatibility remains nonexistent; v0 has no typed way to keep these separate. |

This evidence proposes no schema shape and makes no Foundry edit. It is input
to, not a substitute for, the later Post-implementation Model Evidence Review.

## 10. Independent gate disposition

All required gates A-O are `YES`: authorities pinned, latest Base used, both
real-domain paths exercised, fixture-only work prohibited, only new files,
zero production imports/wiring/state access, 12/12 vectors, no real secrets or
media URLs, no external code copy, no consumer/lifecycle claim, and complete
deletion-only removal.

```text
HUMAN_DISPOSITION = ACCEPT
EVIDENCE_STATUS = ACCEPTED / PROJECT-LOCAL PROTOTYPE EVIDENCE V1
PROTOTYPE = PASS
CORE_CONFORMANCE = 12/12 PASS
RUNTIME_WIRING = NONE
PRODUCT_BEHAVIOR_CHANGED = NO
CURRENT_CONSUMER = NO
CANONICAL_IMPLEMENTATION_CLAIM = NO
LIFECYCLE_EFFECT = NONE
FOUNDRY_SCHEMA_EFFECT = NONE
NEXT = POST-IMPLEMENTATION FOUNDRY MODEL EVIDENCE REVIEW
```
