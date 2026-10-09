"""MCM-SHARE-02. Every fetched news feed is an https RSS on the allowlist and parses."""

from __future__ import annotations

from urllib.parse import urlsplit

from guardian.news.catalog import ALLOWLISTED_FEED_URLS, load_news_sources
from guardian.news.feeds import parse_feed
from guardian.sources.capabilities import news_fetch_capability

PARAGRAPH_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Example DAO</title><link>https://blog.example.invalid</link>
<item><title><![CDATA[Product wind-down notice]]></title>
<link>https://blog.example.invalid/wind-down</link><guid>abc123</guid>
<pubDate>Thu, 01 Oct 2026 01:30:00 GMT</pubDate>
<description><![CDATA[Move your position before the deadline.]]></description>
<content:encoded><![CDATA[<p>Move your position before the deadline.</p>]]></content:encoded></item>
</channel></rss>""".encode("utf-8")


def test_share02_allowlist_matches_catalog_and_is_https_rss_only() -> None:
    sources = load_news_sources()
    fetched = [feed for source in sources.values() for feed in source.feeds if feed.fetch == "allowlisted_get"]
    assert {feed.url for feed in fetched} == set(ALLOWLISTED_FEED_URLS)
    assert all(feed.kind == "rss" and urlsplit(feed.url).scheme == "https" for feed in fetched)
    hosts = set(news_fetch_capability(sources).hosts)
    assert hosts == {urlsplit(url).hostname for url in ALLOWLISTED_FEED_URLS}
    assert "api.paragraph.com" in hosts
    lista = sources["lista"]
    assert any(feed.url == "https://api.paragraph.com/blogs/rss/@blog.lista.org" for feed in lista.feeds)


def test_share02_paragraph_style_feed_parses_to_a_dated_item() -> None:
    parsed = parse_feed(PARAGRAPH_RSS, source_id="lista", source_protocols=("lista",))
    assert parsed.ok is True
    assert len(parsed.items) == 1
    item = parsed.items[0]
    assert item.title == "Product wind-down notice"
    assert item.published_at is not None
    assert item.published_at.year == 2026


def test_share02_html_redirect_stub_is_not_a_feed() -> None:
    stub = b'<a href="https://medium.com/m/global-identity-2?redirectUrl=x">Temporary Redirect</a>.'
    parsed = parse_feed(stub, source_id="uxlink", source_protocols=("uxlink",))
    assert parsed.ok is False
    assert parsed.items == ()
