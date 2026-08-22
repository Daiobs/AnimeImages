# ParseHub Upstream Baseline

## Evidence vocabulary

- **SOURCE-DERIVED FACT**: observed in the pinned upstream source or GitHub metadata.
- **ANIMEIMAGES FACT**: observed in AnimeImages at the recorded baseline.
- **INFERENCE**: a conclusion drawn from static evidence.
- **RECOMMENDATION**: a proposed future action, not current behavior.
- **UNVERIFIED**: not exercised against a live platform or runtime.

## Canonical pin

| Field | Value | Evidence |
| --- | --- | --- |
| Upstream repository | `z-mio/ParseHub` | GitHub repository metadata |
| Default branch | `master` | GitHub repository metadata |
| Pinned commit | `144260f4349c5bb57415f7555a572fcd48c3b8ba` | GitHub commit API and exact archive |
| Pinned tree | `935b46ff35fa2fc4b14ca55220ec25d9a2cbbc26` | GitHub commit API |
| Commit date | `2026-08-21T07:33:44Z` | GitHub commit API |
| Package version | `2.2.0` | pinned `pyproject.toml` |
| License | MIT | pinned `LICENSE` and repository metadata |
| Copyright | `Copyright (c) 2024 梓澪` | pinned `LICENSE` |
| Read method | GitHub API metadata plus exact pinned source archive | No upstream code was imported or executed |

**SOURCE-DERIVED FACT:** At audit time, GitHub `master` resolved to the pinned
commit. `UPSTREAM_HEAD_AHEAD_OF_PIN = NO`. This statement records the audit
window only; future work must query GitHub again and must not silently repin.

## AnimeImages baseline

| Field | Value |
| --- | --- |
| Repository | `Daiobs/AnimeImages` |
| Research base | `82945e65d2a9887149a618bde1c7a524295bc511` |
| Production application runtime (reference only) | `766cecfd239f594e791c556b4caa69aebf2d1f9f` |
| Production checkpoint (reference only) | `checkpoint/production-promotion-ready-v1` |

**ANIMEIMAGES FACT:** The research branch was created from the exact fetched
`origin/main` above with a clean worktree and `0/0` ahead/behind. Production was
not accessed.

## Static acquisition constraints

The archive was inspected as text only. The audit did not install upstream
dependencies, import `parsehub`, run its CLI or tests, access social platforms,
read local credentials, or download media. Runtime and live reliability remain
**UNVERIFIED**.

## Inventory reconciliation

| Inventory source | Count | Finding |
| --- | ---: | --- |
| README support table | 20 rows | Heading says `17+`; table names 20 platforms |
| `Platform` enum | 20 | Matches the parser platform set |
| Registered parser modules | 20 | One registered platform parser per module |
| `provider_api` modules | 17 | Facebook, Snapchat and YouTube use the yt-dlp base without a dedicated provider module |

**SOURCE-DERIVED FACT:** The README table, enum and registered parser modules
agree on the same 20 platform identities. The `17+` label is imprecise but not a
missing-platform defect. The smaller `provider_api` count reflects architecture,
not an automatic capability gap.

## Maintenance signals

- **SOURCE-DERIVED FACT:** The pinned workflow runs Ruff, mypy and offline pytest
  on push and pull requests, and publishes to PyPI from GitHub releases.
- **SOURCE-DERIVED FACT:** The pinned test directory contains four test modules
  and 66 statically discovered test methods.
- **SOURCE-DERIVED FACT:** The package requires Python 3.12 or newer and has a
  broad dependency surface including HTTP clients, HTML/XML parsing, media
  processing, cryptography/signing packages and a development yt-dlp build.
- **UNVERIFIED:** CI status at the pinned commit, package installation, platform
  availability and live extraction success were not executed in this task.

## Update rule

Every future supplier review must record repository, exact commit, tree, version,
license, read method and whether upstream moved. A new supplier commit requires a
new review record; it must never mutate the conclusions attached to this pin.
