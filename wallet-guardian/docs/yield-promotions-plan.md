# Yield promotions: read-only feature plan (not implemented)

Status: plan only. No code, no config, no allowlist entry exists for this feature. Source checks below are HTTP checks made on 2026-10-09 from the Linux box (keyless GET); they show fetchability, not that a parser exists.

## Goal
Tell the owner when a public APY boost event touches something they already hold or have approved. Never a generic promo feed. Never an advice to act.

## Notify gate (hard rule)
An alert is created only when the promotion matches an existing position leg, token balance, or standing approval (same chain, same token or protocol id from the position plugins or approval scan). No match: store nothing user-facing, send nothing. The monitored address is never sent to a promotion source; matching is done locally against public promotion records.

## Alert content (all fields required, else the alert says "unknown")
1. Conditions and deadline: who qualifies (minimum amount, lock/hold period, region or tier limits as written by the publisher), start and end timestamps (UTC and the owner's zone), cap/pool size.
2. APY background: `apyBase` (organic) versus `apyReward` (subsidy/incentive) and the reward token, plus the headline APY as stated by the publisher. Subsidy share = reward / (base + reward). A promo APY is labelled "temporary, subsidy-funded" when the reward part is above half.
3. Mechanism: how the yield is produced (lending interest, staking, emissions, points, CEX-funded bonus), counterparty (custodial CEX product vs onchain contract), whether tokens are locked or redeemable.
4. Safety: contract/product address or official URL from the publisher, audit/age signals only when public, whether approving a contract is required (then link to the existing approval scan), lock-up and withdrawal risk, "not advice". Unverifiable items are shown as `not_verified`, not omitted.

## Sources and fetch check (2026-10-09)
| source | URL | HTTP result | use |
| --- | --- | --- | --- |
| DefiLlama yields | https://yields.llama.fi/pools | 200 JSON, 11.5 MB, 16,703 pools, fields `apyBase`, `apyReward`, `rewardTokens`, `tvlUsd` | fetchable keyless; too large for the 1 MiB feed cap: needs a size-aware JSON adapter with a per-run cap and conditional fetch, or filtering by held protocol/chain |
| DefiLlama protocols | https://api.llama.fi/protocols | 200 JSON, 9.2 MB | maps protocol slug to chain/token/url; same size caveat |
| Binance announcements ("Latest Activities", catalog 93) | https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=93&pageNo=1&pageSize=5 | 200 JSON, titles + `releaseDate` (example titles include Earn APR promotions) | fetchable keyless; undocumented public endpoint, may change; body text needs a second article-detail call (not checked) |
| Binance announcement / Earn / Hold-to-Earn HTML pages | https://www.binance.com/en/support/announcement , /en/earn/hold-to-earn , web3.binance.com/en/earn | 202 empty (WAF challenge) | blocked: do not scrape or bypass; Hold-to-Earn terms are not machine-readable here |
| Binance Wallet announcements | no separate keyless endpoint verified | not verified | gap: use catalog 93 titles only; otherwise manual |
| Pendle blog | https://pendle.medium.com/feed | 200 RSS, 110 KiB | fetchable; fits the existing allowlisted-RSS design |
| Aave governance | already allowlisted | 200 | existing |
| Lista, Kinza, Lido | already/being allowlisted RSS | 200 | existing |
| Venus blog RSS | https://blog.venus.io/rss | no connection (000) | not fetchable; site venus.io itself 200 (HTML) |
| Morpho blog RSS | https://blog.morpho.org/rss/ | no connection from this host (000), morpho.org/blog/ HTML 200 | not fetchable as a feed |
| X / Telegram accounts | n/a | never fetched | manual only |

## Design sketch (for a later MR)
- New module `guardian.yields` with a pure matcher: promotion record -> (chain, protocol id, token) -> held legs/approvals. Records come from (a) DefiLlama pools filtered to held protocol+chain, (b) the Binance catalog-93 JSON, (c) allowlisted RSS items classified by keywords (APY, APR, boost, bonus, reward, campaign).
- Budget: one DefiLlama pools fetch per day with ETag/If-Modified-Since, hard cap 16 MiB, parse streaming; Binance JSON one call per run; failures are `fetch_error`, never "no promotions".
- New coverage row `yield_promotions` with statuses from the existing set; offline fixtures plus real-CLI tests; zero writes; the notify gate above is a tested invariant.
- Snapshot comparison for DefiLlama: an alert on `apyReward` jump needs a stored previous value; first fetch is a quiet baseline.
- Out of scope: auto-joining, deposits, approvals, wallet-address queries to third parties.

## Open questions
- Binance catalog ids beyond 93 and the article-detail endpoint need a second fetch check before any adapter.
- Hold-to-Earn conditions live on WAF-protected pages; until a public machine-readable source exists the alert says "conditions: see publisher page, not fetched".
