"""Parser and state integration tests for yield promotions.

These tests call ``evaluate`` and the fixture parsers directly. They are not CLI
acceptance tests. No live fetch and no send. Socket and HTTP clients are blocked
for this module.
"""

from __future__ import annotations

import gzip
import json
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from guardian.errors import SecurityError
from guardian.jsonschema_lite import validate
from guardian.sources.fixtures import FixtureBackend, RawResponse, request_key
from guardian.storage import Database
from guardian.yields import DECOMPRESSED_CAP, FETCH_URLS, MAX_RECORDS, evaluate
from guardian.yields.parsers import bounded_json, parse_source
from guardian.yields.models import (
    HeldPosition,
    Holdings,
    NoisePolicy,
    StandingApproval,
    load_pool_mapping,
    state_schema,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "yields"
TENANT = "yield-fixtures"
POOLS_URL = FETCH_URLS[0][1]
CATALOG_URL = FETCH_URLS[1][1]
LEND = "11111111-1111-4111-8111-111111111111"
ZERO = "33333333-3333-4333-8333-333333333333"
SMALL = "44444444-4444-4444-8444-444444444444"
EDGE = "55555555-5555-4555-8555-555555555555"
UNMAPPED = "99999999-9999-4999-8999-999999999999"
WALLET_HELD = "00000001-0000-4000-8000-000000000001"
WALLET_APPROVAL = "00000002-0000-4000-8000-000000000002"
WALLET_ZERO = "00000003-0000-4000-8000-000000000003"
SYNTHETIC = r"^0x0{39}[0-9a-f]$"


def addr(n: int) -> str:
    return f"0x{n:040x}"


class Clock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> float:
        return self.moment.timestamp()

    def advance(self, days: int = 1) -> None:
        self.moment += timedelta(days=days)

    @property
    def stamp(self) -> str:
        return self.moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def _books() -> Holdings:
    return Holdings(
        positions=(
            HeldPosition(WALLET_HELD, "bsc-mainnet", "synthetic-lend", (addr(1),), 10**18),
            HeldPosition(WALLET_HELD, "bsc-mainnet", "synthetic-zero", (addr(3),), 5),
            HeldPosition(WALLET_HELD, "bsc-mainnet", "synthetic-small", (addr(4),), 5),
            HeldPosition(WALLET_HELD, "bsc-mainnet", "synthetic-edge", (addr(5),), 5),
            HeldPosition(WALLET_ZERO, "bsc-mainnet", "synthetic-lend", (addr(1),), 0),
        ),
        approvals=(StandingApproval(WALLET_APPROVAL, "bsc-mainnet", addr(6), addr(1), True),),
    )


def _response(body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> RawResponse:
    return RawResponse(status, headers or {}, body)


def _backend(pools: list[RawResponse], catalog: list[RawResponse]) -> FixtureBackend:
    return FixtureBackend({
        request_key("GET", POOLS_URL, b""): pools,
        request_key("GET", CATALOG_URL, b""): catalog,
    })


def _run(database: Database, clock: Clock, backend: FixtureBackend, **kwargs: object) -> dict:
    holdings = kwargs.pop("holdings", None)
    return evaluate(
        database,
        TENANT,
        clock=clock,
        backend=backend,
        resolve=lambda _host: ["1.1.1.1"],
        holdings=_books() if holdings is None else holdings,
        **kwargs,
    )


def _record(report: dict, source: str, record_id: str) -> dict:
    found = [
        row for row in report["records"]
        if row["identity"]["source"] == source and row["identity"]["record_id"] == record_id
    ]
    assert len(found) == 1
    return found[0]


def _matches(report: dict, record_id: str) -> list[dict]:
    return [item for item in report["matches"] if item["record_id"] == record_id]


def _pools_doc(rows: list[dict]) -> bytes:
    return json.dumps({"data": rows}, separators=(",", ":")).encode("utf-8")


def _pool(
    pool_id: str,
    *,
    total: object = None,
    base: object = None,
    reward: object = None,
    unit: str | None = None,
) -> dict:
    row: dict[str, object] = {"pool": pool_id, "apy": total, "apyBase": base, "apyReward": reward}
    if unit is not None:
        row["unit"] = unit
    return row


def _empty_catalog() -> bytes:
    return b'{"articles":[]}'


def test_mcm_yld_01_mapping_approval_only_and_unknown_fields(tmp_path: Path) -> None:
    pools = (FIXTURES / "pools-unknown-fields.json").read_bytes()
    day2 = _pools_doc([
        _pool(LEND, total=5, base=4, reward=None),
        _pool(UNMAPPED, total=9),
        _pool(addr(1), total=3),
    ])
    day3 = _pools_doc([
        _pool(LEND, total=None, base=4, reward=None),
        _pool(UNMAPPED, total=9),
        _pool(addr(1), total=3),
    ])
    catalog = _empty_catalog()
    backend = _backend(
        [_response(pools), _response(day2), _response(day3)],
        [_response(catalog), _response(catalog), _response(catalog)],
    )
    clock = Clock()
    database = Database(tmp_path / "yld-01.sqlite")
    try:
        report = _run(database, clock, backend)
        mapped = _record(report, "defillama_pools", LEND)
        assert mapped["identity"]["chain"] == "bsc-mainnet"
        assert mapped["identity"]["protocol"] == "synthetic-lend"
        assert mapped["identity"]["mapping_status"] == "mapped"
        assert {item["address"] for item in mapped["identity"]["contracts"]} == {addr(1), addr(2)}
        assert all(item["mapped"] is True for item in mapped["identity"]["contracts"])
        assert mapped["identity"]["chain"] != "publisher-chain-ignored"
        assert mapped["yields"]["reported_base_yield"] is None
        assert mapped["yields"]["reported_incentive_reward_yield"] is None
        assert mapped["yields"]["reported_total"] == 5
        assert mapped["yields"]["reported_total_canonical"] == "5"
        assert mapped["yields"]["base_label"] == "reported base yield"
        assert mapped["yields"]["incentive_label"] == "reported incentive/reward yield"
        assert mapped["yields"]["total_label"] == "reported total yield"
        assert mapped["yields"]["reward_token_status"] == "unknown"
        assert mapped["yields"]["reward_token_count_known"] is False
        assert mapped["yields"]["apr_converted_to_apy"] is False
        assert mapped["yields"]["compounding_model"] is None
        assert mapped["observation"]["observed_at"] == clock.stamp
        assert len(mapped["observation"]["content_hash"]) == 64
        assert mapped["comparison_state"]["comparison"] == "baseline"
        assert mapped["comparison_state"]["previous_valid_sample"]["reported_total_canonical"] == "5"
        for group in ("identity", "observation", "yields", "terms", "comparison_state"):
            assert group in mapped
        held = [item for item in _matches(report, LEND) if item["wallet_id"] == WALLET_HELD]
        approval = [item for item in _matches(report, LEND) if item["wallet_id"] == WALLET_APPROVAL]
        assert len(held) == 1
        assert held[0]["eligibility"] == "held_position"
        assert held[0]["via"] == "position"
        assert held[0]["yield_change"] is False
        assert held[0]["statement"] == "Matched a held pool position through the versioned mapping."
        assert len(approval) == 1
        assert approval[0]["eligibility"] == "unknown"
        assert approval[0]["kind"] == "approval_only"
        assert "permission exposure" in approval[0]["statement"]
        assert "Eligibility is unknown." in approval[0]["statement"]
        assert approval[0]["yield_change"] is False
        assert all(item["wallet_id"] != WALLET_ZERO for item in _matches(report, LEND))
        unmapped = _record(report, "defillama_pools", UNMAPPED)
        assert unmapped["identity"]["mapping_status"] == "unmapped"
        assert unmapped["identity"]["chain"] is None
        assert unmapped["identity"]["protocol"] is None
        assert unmapped["identity"]["pool_id"] is None
        assert unmapped["read_coverage"] == "unknown"
        assert _matches(report, UNMAPPED)[0]["statement"] == "Missing mapping means no matched candidate."
        shaped = _record(report, "defillama_pools", addr(1))
        assert shaped["identity"]["mapping_status"] == "unmapped"
        assert all(item["eligibility"] == "not_matched" for item in _matches(report, addr(1)))
        assert not report["candidates"]
        assert all(item["yield_change"] is False for item in report["matches"])
        validate(report["state"], state_schema())

        clock.advance()
        quiet = _run(database, clock, backend)
        adopted = _record(quiet, "defillama_pools", LEND)
        assert adopted["yields"]["reported_base_yield_canonical"] == "4"
        assert adopted["yields"]["reported_total"] == 5
        assert adopted["comparison_state"]["comparison"] == "not_material"
        assert not quiet["candidates"]

        clock.advance()
        missing = _run(database, clock, backend)
        cleared = _record(missing, "defillama_pools", LEND)
        assert cleared["yields"]["reported_total"] is None
        assert cleared["yields"]["reported_base_yield"] == 4
        assert cleared["comparison_state"]["comparison"] == "not_material"
        assert not missing["candidates"]
        assert database.alerts_for(TENANT) == []
        assert database.outbox_for(TENANT) == []
    finally:
        database.close()

    repo_map = json.loads((ROOT / "config" / "yields" / "pool-mapping.v1.json").read_text(encoding="utf-8"))
    packaged_map = json.loads(
        (ROOT / "src" / "guardian" / "_resources" / "config" / "yields" / "pool-mapping.v1.json").read_text(encoding="utf-8")
    )
    assert repo_map == packaged_map
    for name in ("yield-pool-mapping.schema.json", "yield-state.schema.json"):
        repo_schema = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
        packaged_schema = json.loads((ROOT / "src" / "guardian" / "_resources" / "schemas" / name).read_text(encoding="utf-8"))
        assert repo_schema == packaged_schema
    mapping = load_pool_mapping()
    assert repo_map["synthetic"] is True
    assert repo_map["mapping_version"] == "2026-10-10.synthetic.1"
    for pool in mapping.pools:
        assert pool.pool_id.count("-") == 4
        assert not pool.pool_id.startswith("0x")
        assert pool.contracts
        for contract in pool.contracts:
            assert len(contract.address) == 42
            assert contract.address.startswith("0x000000000000000000000000000000000000000")


def test_mcm_yld_02_baseline_thresholds_decreases_and_restart_dedupe(tmp_path: Path) -> None:
    series = [
        [_pool(LEND, total=10), _pool(ZERO, total=0), _pool(SMALL, total=0), _pool(EDGE, base=8, reward=1)],
        [_pool(LEND, total=12), _pool(ZERO, total=2), _pool(SMALL, total=1.5), _pool(EDGE, base=10, reward=4)],
        [_pool(LEND, total=16), _pool(ZERO, total=2), _pool(SMALL, total=1.5), _pool(EDGE, base=10, reward=4)],
        [_pool(LEND, total=16), _pool(ZERO, total=2), _pool(SMALL, total=1.5), _pool(EDGE, base=10, reward=4)],
        [_pool(LEND, total=16), _pool(ZERO, total=2), _pool(SMALL, total=1.5), _pool(EDGE, base=10, reward=4)],
        [_pool(LEND, total=0), _pool(ZERO, total=2), _pool(SMALL, total=1.5), _pool(EDGE, base=10, reward=4)],
        [_pool(LEND, total=0), _pool(ZERO, total=2), _pool(SMALL, total=1.5), _pool(EDGE, base=10, reward=4)],
        [_pool(LEND, total=0), _pool(ZERO, total=2), _pool(SMALL, total=1.5), _pool(EDGE, base=10, reward=4)],
    ]
    catalog = _empty_catalog()
    backend = _backend(
        [_response(_pools_doc(rows)) for rows in series],
        [_response(catalog) for _rows in series],
    )
    clock = Clock()
    path = tmp_path / "yld-02.sqlite"
    database = Database(path)
    try:
        day1 = _run(database, clock, backend)
        assert not day1["candidates"]
        assert _record(day1, "defillama_pools", LEND)["comparison_state"]["comparison"] == "baseline"
        assert _record(day1, "defillama_pools", ZERO)["yields"]["reported_total_canonical"] == "0"

        clock.advance()
        day2 = _run(database, clock, backend)
        assert not day2["candidates"]
        assert _record(day2, "defillama_pools", LEND)["comparison_state"]["comparison"] == "not_material"
        assert _record(day2, "defillama_pools", LEND)["comparison_state"]["previous_valid_sample"]["reported_total_canonical"] == "12"
        assert _record(day2, "defillama_pools", SMALL)["comparison_state"]["comparison"] == "not_material"
        assert _record(day2, "defillama_pools", ZERO)["comparison_state"]["comparison"] == "pending"
        assert _record(day2, "defillama_pools", EDGE)["comparison_state"]["comparison"] == "pending"

        clock.advance()
        day3 = _run(database, clock, backend)
        assert {item["pool_id"] for item in day3["candidates"]} == {ZERO, EDGE}
        assert all(item["wallet_id"] == WALLET_HELD and item["kind"] == "yield_change" for item in day3["candidates"])
        zero = next(item for item in day3["candidates"] if item["pool_id"] == ZERO)
        assert [(item["name"], item["direction"], item["previous"], item["observed"]) for item in zero["components"]] == [
            ("reported_total", "up", "0", "2"),
        ]
        edge = next(item for item in day3["candidates"] if item["pool_id"] == EDGE)
        assert [(item["name"], item["direction"], item["previous"], item["observed"]) for item in edge["components"]] == [
            ("reported_base_yield", "up", "8", "10"),
            ("reported_incentive_reward_yield", "up", "1", "4"),
        ]
        assert edge["components"][0]["label"] == "reported base yield"
        assert edge["components"][1]["label"] == "reported incentive/reward yield"
        assert _record(day3, "defillama_pools", LEND)["comparison_state"]["comparison"] == "pending"

        clock.advance()
        day4 = _run(database, clock, backend)
        assert len(day4["candidates"]) == 1
        held = day4["candidates"][0]
        assert held["pool_id"] == LEND
        assert held["wallet_id"] == WALLET_HELD
        assert [(item["name"], item["direction"], item["previous"], item["observed"]) for item in held["components"]] == [
            ("reported_total", "up", "12", "16"),
        ]
        held_ref = held["ref"]
        calls = len(backend.calls)
        same_day = _run(database, clock, backend)
        assert same_day["status"] == "not_due"
        assert not same_day["candidates"]
        assert len(backend.calls) == calls

        database.close()
        database = Database(path)
        clock.advance()
        day5 = _run(database, clock, backend)
        assert not day5["candidates"]
        assert _record(day5, "defillama_pools", LEND)["comparison_state"]["last_alert"]["ref"] == held_ref

        clock.advance()
        day6 = _run(database, clock, backend)
        assert not day6["candidates"]
        assert _record(day6, "defillama_pools", LEND)["comparison_state"]["comparison"] == "pending"
        assert _record(day6, "defillama_pools", LEND)["comparison_state"]["pending_sample"]["reported_total_canonical"] == "0"
        assert _record(day6, "defillama_pools", LEND)["comparison_state"]["previous_valid_sample"]["reported_total_canonical"] == "16"

        clock.advance()
        day7 = _run(database, clock, backend)
        assert len(day7["candidates"]) == 1
        down = day7["candidates"][0]
        assert down["pool_id"] == LEND
        assert [(item["direction"], item["previous"], item["observed"]) for item in down["components"]] == [("down", "16", "0")]
        down_ref = down["ref"]

        database.close()
        database = Database(path)
        clock.advance()
        day8 = _run(database, clock, backend)
        assert not day8["candidates"]
        assert _record(day8, "defillama_pools", LEND)["comparison_state"]["last_alert"]["ref"] == down_ref
        assert all(item["wallet_id"] != WALLET_APPROVAL for item in day3["candidates"] + day4["candidates"] + day7["candidates"])
        assert database.alerts_for(TENANT) == []
        assert database.outbox_for(TENANT) == []
    finally:
        database.close()

    wide = NoisePolicy(Decimal("100"), Decimal("0.25"))
    quiet_days = [[_pool(LEND, total=8)], [_pool(LEND, total=10)], [_pool(LEND, total=10)]]
    quiet_backend = _backend(
        [_response(_pools_doc(rows)) for rows in quiet_days],
        [_response(catalog) for _rows in quiet_days],
    )
    quiet_clock = Clock()
    quiet_db = Database(tmp_path / "yld-02-policy.sqlite")
    try:
        reports = []
        for _index in range(3):
            reports.append(_run(quiet_db, quiet_clock, quiet_backend, policy=wide))
            quiet_clock.advance()
        assert all(not item["candidates"] for item in reports)
        assert reports[-1]["state"]["policy"]["min_absolute_points"] == "100"
        assert reports[-1]["state"]["policy"]["risk_guarantee"] is False
        assert reports[-1]["state"]["policy"]["note"] == "configurable product policy, not a risk guarantee"
        assert _record(reports[-1], "defillama_pools", LEND)["comparison_state"]["comparison"] == "not_material"
    finally:
        quiet_db.close()


def test_mcm_yld_03_timezone_terms_and_spoofed_campaigns(tmp_path: Path) -> None:
    pools = (FIXTURES / "pools-unknown-fields.json").read_bytes()
    catalog = (FIXTURES / "catalog-93-title.json").read_bytes()
    promotions = (FIXTURES / "promotions-mixed.json").read_bytes()
    backend = _backend([_response(pools)], [_response(catalog)])
    clock = Clock()
    database = Database(tmp_path / "yld-03.sqlite")
    try:
        report = _run(database, clock, backend, promotions=promotions)
        assert report["fetched_urls"] == (POOLS_URL, CATALOG_URL)
        assert all("articles.invalid" not in url for url in report["fetched_urls"])
        assert all(item["kind"] == "promotion_event" for item in report["candidates"])
        assert {item["record_id"] for item in report["candidates"]} == {
            "deadline-1",
            "ambiguous-1",
            "apr-1",
            "high-reward-1",
        }
        assert {item["wallet_id"] for item in report["candidates"]} == {WALLET_HELD}
        assert all(item["source"] == "explicit_body" for item in report["candidates"])
        assert all(item["eligibility"] == "held_position" for item in report["candidates"])
        deadline = _record(report, "explicit_body", "deadline-1")
        ends = [item for item in deadline["terms"]["deadlines"] if item["role"] == "end"]
        assert len(ends) == 1
        assert ends[0]["timezone"] == "+08:00"
        assert ends[0]["utc"] == "2026-12-01T08:00:00Z"
        assert ends[0]["original"] == "2026-12-01T16:00:00+08:00"
        assert "until" in ends[0]["quote"]
        assert "2026-12-01T16:00:00+08:00" in ends[0]["quote"]
        assert "1999-01-01" not in ends[0]["original"]
        assert all("1999-01-01" not in item["original"] for item in deadline["terms"]["deadlines"])
        assert deadline["observation"]["published_at"] == "2026-10-01T00:00:00Z"
        assert deadline["terms"]["conditions"]["minimum_amount"] == "100"
        assert deadline["terms"]["conditions"]["minimum_unit"] == "USDT"
        assert deadline["terms"]["conditions"]["lock_days"] == 14
        assert deadline["terms"]["residual_conditions"] == "none"
        provenance = {item["field"]: item["quote"] for item in deadline["terms"]["provenance"]}
        assert "locked for 14 days" in provenance["lock_days"]
        assert "100" in provenance["minimum_amount"]
        assert "USDT" in provenance["minimum_amount"]
        assert "until" in provenance["deadline"]
        assert "2026-12-01T16:00:00+08:00" in provenance["deadline"]
        assert deadline["yields"]["described_as_temporary"] is True
        assert deadline["yields"]["temporary_language"] == "publisher_stated_duration"
        assert deadline["terms"]["links_not_followed"] >= 1
        held = [item for item in _matches(report, "deadline-1") if item["wallet_id"] == WALLET_HELD]
        assert len(held) == 1
        assert held[0]["eligibility"] == "held_position"
        assert held[0]["yield_change"] is False
        assert held[0]["pool_id"] == LEND

        ambiguous = _record(report, "explicit_body", "ambiguous-1")
        assert ambiguous["terms"]["deadlines"] == []
        assert ambiguous["terms"]["extraction_status"] == "ambiguous"

        spoof = _record(report, "explicit_body", "spoof-1")
        assert spoof["identity"]["mapping_status"] == "spoofed"
        assert spoof["identity"]["contracts"] == []
        assert spoof["identity"]["pool_id"] is None
        assert spoof["identity"]["claimed_contracts"] == [addr(15)]
        spoof_match = _matches(report, "spoof-1")
        assert spoof_match[0]["kind"] == "spoofed"
        assert spoof_match[0]["yield_change"] is False
        assert spoof_match[0]["statement"] == "The campaign contract does not match the trusted mapping."

        protocol = _record(report, "explicit_body", "protocol-only-1")
        assert protocol["terms"]["deadlines"]
        assert protocol["identity"]["mapping_status"] == "campaign_required"
        assert _matches(report, "protocol-only-1")[0]["kind"] == "campaign_required"
        assert _matches(report, "protocol-only-1")[0]["eligibility"] == "not_matched"
        assert "supported campaign" in _matches(report, "protocol-only-1")[0]["statement"]

        unknown = _record(report, "explicit_body", "unknown-campaign-1")
        assert unknown["identity"]["mapping_status"] == "unsupported"
        assert _matches(report, "unknown-campaign-1")[0]["kind"] == "unsupported"

        rejected = _record(report, "explicit_body", "rejected-claim-1")
        assert rejected["identity"]["mapping_status"] == "unsupported"
        assert rejected["identity"]["claimed_contracts"] == []
        assert "00aa" not in json.dumps(rejected["identity"])

        apr = _record(report, "explicit_body", "apr-1")
        assert apr["yields"]["unit"] == "APR"
        assert apr["yields"]["reported_total"] == 12
        assert apr["yields"]["reported_base_yield"] is None
        assert apr["yields"]["reported_incentive_reward_yield"] is None
        assert apr["yields"]["apr_converted_to_apy"] is False
        assert apr["yields"]["compounding_model"] is None

        heavy = _record(report, "explicit_body", "high-reward-1")
        assert heavy["yields"]["described_as_temporary"] is False
        assert heavy["yields"]["temporary_language"] == "not_described_as_temporary"
        assert heavy["yields"]["reported_total"] is None
        assert heavy["yields"]["reported_base_yield"] == 1
        assert heavy["yields"]["reported_incentive_reward_yield"] == 9
        dumped = json.dumps(heavy)
        assert "subsidy-funded" not in dumped
        assert "guaranteed" not in dumped

        article = _record(report, "binance_catalog_93", "synthetic-article-1")
        assert article["terms"]["extraction_status"] == "title_only"
        assert article["terms"]["deadlines"] == []
        assert article["terms"]["catalog_status"] == "undocumented"
        assert article["terms"]["article_detail"] == "not_observable"
        assert article["observation"]["published_at"] == "2026-10-08T00:00:00Z"
        assert article["read_coverage"] == "not_observable"
        assert article["yields"]["unit"] == "unknown"
        assert article["yields"]["reported_total"] is None
        catalog_match = _matches(report, "synthetic-article-1")[0]
        assert catalog_match["kind"] == "campaign_required"
        assert catalog_match["eligibility"] == "not_matched"
        assert "2026-11-01" in article["terms"]["excerpt"]
        assert report["blocked_sources"][0]["source"] == "binance_hold_to_earn_html"
        assert report["blocked_sources"][0]["fetched"] is False
        assert report["blocked_sources"][0]["status"] == "blocked"
    finally:
        database.close()


def test_mcm_yld_04_caps_truncated_nan_and_304_without_cache(tmp_path: Path) -> None:
    catalog = _empty_catalog()
    small = _pools_doc([_pool(LEND, total=4)])
    gzipped = gzip.compress(small)
    clock = Clock()
    database = Database(tmp_path / "yld-04-gzip.sqlite")
    try:
        parsed = _run(database, clock, _backend([_response(gzipped)], [_response(catalog)]))
        assert _record(parsed, "defillama_pools", LEND)["yields"]["reported_total"] == 4
        assert parsed["status"] == "ok"
    finally:
        database.close()

    huge = b" " * (DECOMPRESSED_CAP + 1)
    bomb = gzip.compress(b"0" * (DECOMPRESSED_CAP + 1))
    assert len(bomb) < DECOMPRESSED_CAP
    nested = "0"
    for _index in range(100):
        nested = '{"a":' + nested + "}"
    too_many = json.dumps({"data": [0] * (MAX_RECORDS + 1)}, separators=(",", ":")).encode("utf-8")
    cases = [
        (huge, "too_large", "decompressed_cap"),
        (bomb, "too_large", "decompressed_cap"),
        (nested.encode("utf-8"), "rejected", "nesting_limit"),
        (too_many, "rejected", "record_limit"),
        ((FIXTURES / "truncated.json").read_bytes(), "rejected", "truncated"),
        ((FIXTURES / "nan.json").read_bytes(), "rejected", "nan"),
    ]
    for index, (body, read_status, reason) in enumerate(cases):
        case_db = Database(tmp_path / f"yld-04-case-{index}.sqlite")
        try:
            report = _run(case_db, Clock(), _backend([_response(body)], [_response(catalog)]))
            source = report["state"]["sources"]["defillama_pools"]
            assert source["read_status"] == read_status
            assert source["reason"] == reason
            assert report["records"] == []
            assert not any(item.get("reported_total") == 0 for item in report["records"])
            assert len(report["records"]) != MAX_RECORDS
        finally:
            case_db.close()

    bare = Database(tmp_path / "yld-04-304.sqlite")
    try:
        missing = _run(
            bare,
            Clock(),
            _backend([_response(b"", 304, {"etag": '"v1"'})], [_response(catalog)]),
        )
        source = missing["state"]["sources"]["defillama_pools"]
        assert source["read_status"] == "cache_missing"
        assert source["reason"] == "304_without_cache"
        assert source["cached"] is False
        assert source["etag"] is None
        assert missing["records"] == []
    finally:
        bare.close()

    cached = _pools_doc([_pool(LEND, total=7)])
    clock = Clock()
    backend = _backend(
        [_response(cached, headers={"etag": '"v1"'}), _response(b"", 304, {"etag": '"v1"'})],
        [_response(catalog), _response(catalog)],
    )
    database = Database(tmp_path / "yld-04-cache.sqlite")
    try:
        first = _run(database, clock, backend)
        assert _record(first, "defillama_pools", LEND)["yields"]["reported_total"] == 7
        observed = _record(first, "defillama_pools", LEND)["observation"]["observed_at"]
        assert _record(first, "defillama_pools", LEND)["comparison_state"]["comparison"] == "baseline"
        clock.advance()
        second = _run(database, clock, backend)
        kept = _record(second, "defillama_pools", LEND)
        assert second["state"]["sources"]["defillama_pools"]["read_status"] == "not_modified"
        assert kept["yields"]["reported_total"] == 7
        assert kept["observation"]["observed_at"] == observed
        assert kept["comparison_state"]["comparison"] == "baseline"
        pools_calls = [call for call in backend.calls if call.url == POOLS_URL]
        assert len(pools_calls) == 2
        assert ("if-none-match", '"v1"') in pools_calls[1].public_headers
        assert all(name != "if-none-match" for name, _value in pools_calls[0].public_headers)
    finally:
        database.close()


def test_mcm_yld_05_zero_live_fetches_and_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)
    body = _pools_doc([_pool(LEND, total=4)])
    catalog = _empty_catalog()
    backend = _backend([_response(body), _response(body)], [_response(catalog), _response(catalog)])
    clock = Clock()
    database = Database(tmp_path / "yld-05.sqlite")
    try:
        report = _run(database, clock, backend)
        assert _record(report, "defillama_pools", LEND)["yields"]["reported_total"] == 4
        assert report["sent"] is False
        assert report["enabled"] is False
        assert report["scheduler"] is False
        assert report["live_fetch"] is False
        assert report["fetched_urls"] == (POOLS_URL, CATALOG_URL)
        assert database.alerts_for(TENANT) == []
        assert database.outbox_for(TENANT) == []
        assert len(backend.calls) == 2
        assert {call.method for call in backend.calls} == {"GET"}
        assert {call.url for call in backend.calls} == {POOLS_URL, CATALOG_URL}
        for call in backend.calls:
            names = {name for name, _value in call.public_headers}
            assert "authorization" not in names
            assert ("user-agent", "WalletGuardianYieldFixtures/1") in call.public_headers
        calls = len(backend.calls)
        again = _run(database, clock, backend)
        assert again["status"] == "not_due"
        assert len(backend.calls) == calls
        with pytest.raises(SecurityError) as raised:
            _run(database, clock, backend, sender=object())
        assert raised.value.reason == "send_disabled"
        assert len(backend.calls) == calls
    finally:
        database.close()

    banned = ("httpx", "eth_send", "llama.fi", "binance.com", "HttpxBackend")
    for path in (ROOT / "src" / "guardian" / "yields").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text
    for name in ("pipeline.py", "cli.py", "scheduler.py", "service.py"):
        text = (ROOT / "src" / "guardian" / name).read_text(encoding="utf-8")
        assert "guardian.yields" not in text
        assert "yield_promotions" not in text


def _series(tmp_path: Path, name: str, days: list[list[dict]], **kwargs: object) -> list[dict]:
    catalog = _empty_catalog()
    backend = _backend(
        [_response(_pools_doc(rows)) for rows in days],
        [_response(catalog) for _rows in days],
    )
    clock = Clock()
    database = Database(tmp_path / name)
    reports: list[dict] = []
    try:
        for _index in range(len(days)):
            reports.append(_run(database, clock, backend, **kwargs))
            clock.advance()
    finally:
        database.close()
    return reports


def test_mcm_yld_01_held_position_requires_protocol(tmp_path: Path) -> None:
    """Parser/state integration: a hold is protocol plus chain plus a mapped contract."""
    wrong = "00000011-0000-4000-8000-000000000011"
    right = "00000012-0000-4000-8000-000000000012"
    protocol_only = "00000013-0000-4000-8000-000000000013"
    holdings = Holdings(positions=(
        HeldPosition(wrong, "bsc-mainnet", "synthetic-other", (addr(1),), 10**18),
        HeldPosition(right, "bsc-mainnet", "synthetic-lend", (addr(1),), 10**18),
        HeldPosition(protocol_only, "bsc-mainnet", "synthetic-lend", (addr(3),), 10**18),
    ))
    backend = _backend(
        [_response(_pools_doc([_pool(LEND, total=5), _pool(ZERO, total=1)]))],
        [_response(_empty_catalog())],
    )
    database = Database(tmp_path / "yld-01-protocol.sqlite")
    try:
        report = _run(database, Clock(), backend, holdings=holdings)
        lend = _matches(report, LEND)
        zero = _matches(report, ZERO)
        assert [item["wallet_id"] for item in lend if item["eligibility"] == "held_position"] == [right]
        assert lend[0]["eligibility"] == "held_position"
        assert lend[0]["via"] == "position"
        assert lend[0]["pool_id"] == LEND
        assert all(item["wallet_id"] != wrong for item in lend + zero)
        assert all(item["wallet_id"] != protocol_only for item in lend + zero)
        assert not report["candidates"]
    finally:
        database.close()


def test_mcm_yld_02_refuse_unknown_or_incompatible_units(tmp_path: Path) -> None:
    """Parser/state integration: unknown or mismatched units reset and do not emit."""
    crossed = _series(tmp_path, "yld-02-units.sqlite", [
        [_pool(LEND, total=8, unit="APY")],
        [_pool(LEND, total=12, unit="APR")],
        [_pool(LEND, total=12, unit="APR")],
    ])
    assert all(not item["candidates"] for item in crossed)
    reset = _record(crossed[1], "defillama_pools", LEND)
    assert reset["comparison_state"]["comparison"] == "baseline"
    assert reset["comparison_state"]["pending_sample"] is None
    assert reset["comparison_state"]["previous_valid_sample"]["unit"] == "APR"
    assert reset["comparison_state"]["previous_valid_sample"]["reported_total_canonical"] == "12"
    settled = _record(crossed[2], "defillama_pools", LEND)
    assert settled["comparison_state"]["comparison"] == "not_material"
    assert settled["yields"]["unit"] == "APR"
    assert settled["yields"]["reported_total"] == 12

    unknown = _series(tmp_path, "yld-02-unknown-unit.sqlite", [
        [_pool(LEND, total=8, unit="unknown")],
        [_pool(LEND, total=12, unit="unknown")],
        [_pool(LEND, total=12, unit="unknown")],
    ])
    assert all(not item["candidates"] for item in unknown)
    assert _record(unknown[2], "defillama_pools", LEND)["comparison_state"]["comparison"] == "baseline"
    assert _record(unknown[2], "defillama_pools", LEND)["yields"]["unit"] == "unknown"

    same_unit = _series(tmp_path, "yld-02-apr.sqlite", [
        [_pool(LEND, total=8, unit="APR")],
        [_pool(LEND, total=12, unit="APR")],
        [_pool(LEND, total=12, unit="APR")],
    ])
    assert not same_unit[0]["candidates"]
    assert not same_unit[1]["candidates"]
    assert len(same_unit[2]["candidates"]) == 1
    change = same_unit[2]["candidates"][0]
    assert change["kind"] == "yield_change"
    assert change["wallet_id"] == WALLET_HELD
    assert change["pool_id"] == LEND
    assert change["unit"] == "APR"
    assert [(item["name"], item["direction"], item["previous"], item["observed"]) for item in change["components"]] == [
        ("reported_total", "up", "8", "12"),
    ]


def _body(record_id: str, body: str, apy: int) -> dict[str, object]:
    return {
        "record_id": record_id,
        "campaign_id": "synthetic-lend-boost-1",
        "unit": "APY",
        "apy": apy,
        "body": body,
    }


def test_mcm_yld_02_promotion_event_separate_from_pool_yield(tmp_path: Path) -> None:
    """Parser/state integration: announcement figures are promotion events, not pool yields."""
    text = "Eligible until 2026-12-01T00:00:00Z."
    revised = "Eligible until 2026-12-15T00:00:00Z."
    days = [
        _body("ann-1", text, 8),
        _body("ann-1", text, 8),
        _body("ann-1", text, 12),
        _body("ann-1", revised, 12),
        _body("ann-1", revised, 12),
    ]
    catalog = _empty_catalog()
    pools = _pools_doc([_pool(LEND, total=5)])
    backend = _backend(
        [_response(pools) for _row in days],
        [_response(catalog) for _row in days],
    )
    clock = Clock()
    path = tmp_path / "yld-02-promo.sqlite"
    database = Database(path)
    try:
        first = _run(database, clock, backend, promotions=json.dumps({"records": [days[0]]}).encode("utf-8"))
        assert len(first["candidates"]) == 1
        event = first["candidates"][0]
        assert event["kind"] == "promotion_event"
        assert event["source"] == "explicit_body"
        assert event["record_id"] == "ann-1"
        assert event["wallet_id"] == WALLET_HELD
        assert event["pool_id"] == LEND
        assert event["eligibility"] == "held_position"
        assert event["unit"] == "APY"
        assert "components" not in event
        assert first["sent"] is False
        assert first["scheduler"] is False
        assert database.alerts_for(TENANT) == []
        assert database.outbox_for(TENANT) == []
        semantic = _record(first, "explicit_body", "ann-1")["comparison_state"]["dedupe"]["last_semantic"]
        assert isinstance(semantic, str) and len(semantic) == 64
        assert _record(first, "explicit_body", "ann-1")["comparison_state"]["comparison"] != "sustained"
        assert _record(first, "defillama_pools", LEND)["comparison_state"]["comparison"] == "baseline"

        clock.advance()
        repeat = _run(database, clock, backend, promotions=json.dumps({"records": [days[1]]}).encode("utf-8"))
        assert not repeat["candidates"]
        assert _record(repeat, "explicit_body", "ann-1")["comparison_state"]["comparison"] == "duplicate"
        assert _record(repeat, "explicit_body", "ann-1")["comparison_state"]["dedupe"]["last_semantic"] == semantic

        clock.advance()
        revised_rate = _run(database, clock, backend, promotions=json.dumps({"records": [days[2]]}).encode("utf-8"))
        assert [item["kind"] for item in revised_rate["candidates"]] == ["promotion_event"]
        assert revised_rate["candidates"][0]["record_id"] == "ann-1"
        assert _record(revised_rate, "explicit_body", "ann-1")["comparison_state"]["comparison"] != "sustained"
        rate_semantic = _record(revised_rate, "explicit_body", "ann-1")["comparison_state"]["dedupe"]["last_semantic"]
        assert rate_semantic != semantic

        clock.advance()
        changed = _run(database, clock, backend, promotions=json.dumps({"records": [days[3]]}).encode("utf-8"))
        assert [item["kind"] for item in changed["candidates"]] == ["promotion_event"]
        assert changed["candidates"][0]["dedup_key"] != event["dedup_key"]
        changed_semantic = _record(changed, "explicit_body", "ann-1")["comparison_state"]["dedupe"]["last_semantic"]
        assert changed_semantic not in {semantic, rate_semantic}

        database.close()
        database = Database(path)
        clock.advance()
        quiet = _run(database, clock, backend, promotions=json.dumps({"records": [days[4]]}).encode("utf-8"))
        assert not quiet["candidates"]
        assert _record(quiet, "explicit_body", "ann-1")["comparison_state"]["dedupe"]["last_semantic"] == changed_semantic
        assert _record(quiet, "defillama_pools", LEND)["yields"]["reported_total"] == 5
        assert database.alerts_for(TENANT) == []
        assert database.outbox_for(TENANT) == []
    finally:
        database.close()


def test_mcm_yld_03_negated_and_unparsed_terms_stay_unknown(tmp_path: Path) -> None:
    """Parser/state integration: negation, conflict, and unparsed limits stay unknown."""
    stated = (
        "Minimum 100 USDT locked for 14 days. "
        "Eligible until 2026-12-01T16:00:00+08:00."
    )
    restricted = (
        "Minimum 100 USDT locked for 14 days. New users must complete KYC. "
        "Eligible until 2026-12-01T00:00:00Z."
    )
    document = {
        "records": [
            {"record_id": "stated-lock-1", "body": stated},
            {"record_id": "restricted-1", "body": restricted},
            {"record_id": "negated-lock-1", "body": "Funds are not locked for 14 days."},
            {
                "record_id": "conflict-lock-1",
                "body": "Funds are locked for 14 days. Funds are not locked for 14 days.",
            },
            {
                "record_id": "conflict-days-1",
                "body": "Funds are locked for 7 days and locked for 14 days.",
            },
        ],
    }
    parsed = parse_source("explicit_body", json.dumps(document).encode("utf-8"))
    assert parsed.ok is True
    by_id = {item.record_id: item for item in parsed.records}

    positive = by_id["stated-lock-1"]
    assert positive.conditions["lock_days"] == 14
    assert positive.conditions["minimum_amount"] == "100"
    assert positive.conditions["minimum_unit"] == "USDT"
    assert positive.residual_conditions == "none"
    assert positive.extraction_status == "extracted"
    quotes = {item["field"]: item["quote"] for item in positive.provenance}
    assert "locked for 14 days" in quotes["lock_days"]
    assert "100" in quotes["minimum_amount"] and "USDT" in quotes["minimum_amount"]
    assert "until" in quotes["deadline"]
    assert quotes["lock_days"].startswith('"')
    assert {item["source"] for item in positive.provenance} == {"explicit_body"}

    kyc = by_id["restricted-1"]
    assert kyc.conditions["minimum_amount"] == "100"
    assert kyc.conditions["lock_days"] == 14
    assert kyc.residual_conditions == "unknown"
    assert any(item["field"] == "lock_days" for item in kyc.provenance)

    negated = by_id["negated-lock-1"]
    assert negated.conditions["lock_days"] is None
    assert negated.extraction_status == "unknown"
    assert negated.residual_conditions == "unknown"
    assert negated.duration_stated is False
    assert negated.described_as_temporary is False
    assert all(item["field"] != "lock_days" for item in negated.provenance)

    conflict = by_id["conflict-lock-1"]
    assert conflict.conditions["lock_days"] is None
    assert conflict.residual_conditions == "unknown"
    assert all(item["field"] != "lock_days" for item in conflict.provenance)
    assert by_id["conflict-days-1"].conditions["lock_days"] is None
    assert by_id["conflict-days-1"].residual_conditions == "unknown"

    backend = _backend(
        [_response(_pools_doc([_pool(LEND, total=5)]))],
        [_response(_empty_catalog())],
    )
    database = Database(tmp_path / "yld-03-terms.sqlite")
    try:
        report = _run(database, Clock(), backend, promotions=json.dumps(document).encode("utf-8"))
        stored = _record(report, "explicit_body", "restricted-1")
        assert stored["terms"]["residual_conditions"] == "unknown"
        assert stored["terms"]["conditions"]["lock_days"] == 14
        kept = _record(report, "explicit_body", "negated-lock-1")
        assert kept["terms"]["conditions"]["lock_days"] is None
        assert kept["terms"]["residual_conditions"] == "unknown"
        assert not any(item["kind"] == "yield_change" for item in report["candidates"])
        assert database.alerts_for(TENANT) == []
        assert database.outbox_for(TENANT) == []
        validate(report["state"], state_schema())
    finally:
        database.close()


def _nested(minimum: int) -> bytes:
    nested = "0"
    while len(nested) < minimum:
        nested = '{"a":' + nested + "}"
    return nested.encode("utf-8")


def test_mcm_yld_04_gzip_completion_depth_and_prior_observation(tmp_path: Path) -> None:
    """Parser/state integration: bad streams fail closed and keep the prior sample."""
    catalog = _empty_catalog()
    good = _pools_doc([_pool(LEND, total=4)])
    gzipped = gzip.compress(good)
    parsed_doc, parsed_reason = bounded_json(gzipped)
    assert parsed_reason == ""
    assert parsed_doc["data"][0]["apy"] == 4

    trimmed = gzipped[:-8]
    trimmed_doc, trimmed_reason = bounded_json(trimmed)
    assert trimmed_doc is None
    assert trimmed_reason == "truncated"

    concatenated = gzipped + gzip.compress(_pools_doc([_pool(LEND, total=99)]))
    extra_doc, extra_reason = bounded_json(concatenated)
    assert extra_doc is None
    assert extra_reason == "trailing_data"

    deep = _nested(20_000)
    assert len(deep) >= 20_000
    deep_doc, deep_reason = bounded_json(deep)
    assert deep_doc is None
    assert deep_reason == "nesting_limit"

    cases = [
        (trimmed, "truncated"),
        (concatenated, "trailing_data"),
        (deep, "nesting_limit"),
    ]
    for index, (body, reason) in enumerate(cases):
        database = Database(tmp_path / f"yld-04-keep-{index}.sqlite")
        backend = _backend(
            [_response(good), _response(body)],
            [_response(catalog), _response(catalog)],
        )
        clock = Clock()
        try:
            first = _run(database, clock, backend)
            assert _record(first, "defillama_pools", LEND)["yields"]["reported_total"] == 4
            assert first["status"] == "ok"
            clock.advance()
            second = _run(database, clock, backend)
            source = second["state"]["sources"]["defillama_pools"]
            assert source["read_status"] == "rejected"
            assert source["reason"] == reason
            kept = _record(second, "defillama_pools", LEND)
            assert kept["yields"]["reported_total"] == 4
            assert kept["yields"]["reported_total_canonical"] == "4"
            assert kept["comparison_state"]["comparison"] == "baseline"
            assert kept["comparison_state"]["previous_valid_sample"]["reported_total_canonical"] == "4"
            assert second["records"] != []
            assert not any(item.get("reported_total") == 0 for item in second["records"])
        finally:
            database.close()
