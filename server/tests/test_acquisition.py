import socket
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from oevira.acquisition import AcquisitionError, _read_public, _readable_html, _read_feed, acquire_url, open_pinned_socket, public_target


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]


class AcquisitionTests(unittest.TestCase):
    def test_synthetic_proxy_dns_requires_newly_verified_public_answers(self):
        synthetic = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.12", 443))]
        import dns.resolver
        with patch("oevira.acquisition.socket.getaddrinfo", return_value=synthetic), patch("oevira.acquisition.getproxies", return_value={"https":"http://127.0.0.1:7890"}), patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve.side_effect = [[SimpleNamespace(address="93.184.215.14")], dns.resolver.NoAnswer()]
            self.assertEqual(public_target("https://example.com/")[-1], "93.184.215.14")
            resolver.return_value.resolve.side_effect = [[SimpleNamespace(address="127.0.0.1")], dns.resolver.NoAnswer()]
            with self.assertRaises(AcquisitionError) as error:
                public_target("https://example.com/")
            self.assertEqual(error.exception.code, "PRIVATE_SOURCE_DENIED")

    def test_existing_proxy_tunnel_connects_to_numeric_pin_without_reresolving(self):
        response = iter(bytes([value]) for value in b"HTTP/1.1 200 Connection established\r\n\r\n")
        with patch("oevira.acquisition.getproxies", return_value={"https":"http://127.0.0.1:7890"}), patch("oevira.acquisition.socket.socket") as factory, patch("oevira.acquisition.socket.getaddrinfo", side_effect=AssertionError("No second resolution")):
            factory.return_value.recv.side_effect = lambda _count: next(response)
            connection = open_pinned_socket("93.184.215.14", 443, 5)
            connection.connect.assert_called_once_with(("127.0.0.1", 7890))
            self.assertIn(b"CONNECT 93.184.215.14:443 HTTP/1.1", connection.sendall.call_args.args[0])
            self.assertNotIn(b"Authorization", connection.sendall.call_args.args[0])

    def test_declared_primary_content_preserves_source_links_and_excludes_hidden_noise(self):
        content = '''<html><head><title>预算公告</title>
        <link rel="alternate" type="application/rss+xml" title="新闻Feed" href="/feed.xml"></head><body>
        <nav>换肤与登录</nav><main><article>
        <h1>本期预算</h1><p>金额 <b>1,200</b> 元。</p><p hidden>秘密草稿</p>
        <a href="/report?q=1#page">完整报告</a><script>99999</script>
        </article></main><footer>不断变化的脚本版本</footer></body></html>'''
        result = _readable_html(content, "https://example.com/current")
        self.assertEqual(result["extraction_scope"], "declared_primary_content")
        self.assertIn("金额 1,200 元。", result["text"])
        self.assertEqual(result["text"].count("本期预算"), 1)
        self.assertNotIn("草稿", result["text"])
        self.assertNotIn("换肤", result["text"])
        self.assertNotIn("99999", result["text"])
        self.assertEqual(result["discovered_links"], [
            {"url": "https://example.com/feed.xml", "title": "新闻Feed", "same_origin": True, "kind": "declared_feed"},
            {"url": "https://example.com/report?q=1", "title": "完整报告", "same_origin": True, "kind": "page_link"},
        ])

    def test_atom_entries_keep_native_dates_and_observed_pagination(self):
        result = _read_feed('''<feed xmlns="http://www.w3.org/2005/Atom"><title>观察源</title>
        <link rel="next" href="?page=2"/><entry><id>tag:source:item1</id><title>方案更新</title>
        <link href="/entry/1"/><author><name>来源作者</name></author><published>2026-01-01T10:00:00Z</published>
        <summary type="html">&lt;p&gt;第一条&lt;/p&gt;</summary></entry></feed>'''.encode(), "https://example.com/feed")
        self.assertEqual(result["title"], "观察源")
        self.assertEqual(result["entries"][0]["published_at"], "2026-01-01T10:00:00Z")
        self.assertEqual(result["entries"][0]["native_id"], "tag:source:item1")
        self.assertEqual(result["entries"][0]["text"], "第一条")
        self.assertEqual(result["entries"][0]["author"], "来源作者")
        self.assertEqual(result["next_url"], "https://example.com/feed?page=2")
        self.assertNotIn("<feed", result["text"])

    def test_feed_rejects_external_entities_and_reports_bounded_partial_results(self):
        with self.assertRaises(AcquisitionError) as result:
            _read_feed(b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///private">]><rss><channel><title>&x;</title></channel></rss>', "https://example.com/feed")
        self.assertEqual(result.exception.code, "SOURCE_FEED_INVALID")
        feed = b"<rss><channel><title>feed</title>" + b"<item><title>item</title></item>" * 501 + b"</channel></rss>"
        result = _read_feed(feed, "https://example.com/feed")
        self.assertTrue(result["entries_truncated"])
        self.assertEqual(result["entry_count_observed"], 501)
        self.assertEqual(len(result["entries"]), 500)

    def test_sitemaps_project_only_bounded_url_candidates(self):
        urlset = _read_feed(
            b'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/news/one</loc><lastmod>2026-11-10</lastmod></url>
              <url><loc>https://other.example/two</loc></url>
            </urlset>''',
            "https://example.com/sitemap.xml",
        )
        self.assertEqual(urlset["extraction_scope"], "sitemap_urls")
        self.assertEqual(urlset["entry_count_observed"], 2)
        self.assertEqual(urlset["entries"][0]["url"], "https://example.com/news/one")
        self.assertEqual(urlset["entries"][0]["kind"], "sitemap_url")
        self.assertIn("2026-11-10", urlset["text"])
        self.assertEqual(urlset["discovered_links"], [])

        index = _read_feed(
            b'''<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <sitemap><loc>https://example.com/news-sitemap.xml</loc></sitemap>
            </sitemapindex>''',
            "https://example.com/sitemap-index.xml",
        )
        self.assertEqual(index["entries"][0]["kind"], "sitemap_index")
        self.assertEqual(index["entries"][0]["url"], "https://example.com/news-sitemap.xml")

        bounded = b'<urlset>' + b"<url><loc>https://example.com/a</loc></url>" * 501 + b'</urlset>'
        partial = _read_feed(bounded, "https://example.com/sitemap.xml")
        self.assertTrue(partial["entries_truncated"])
        self.assertTrue(partial["links_truncated"])
        self.assertEqual(partial["entry_count_observed"], 501)
        self.assertEqual(len(partial["entries"]), 500)

    @patch("oevira.acquisition.socket.getaddrinfo", return_value=PUBLIC)
    def test_robots_sitemaps_and_declared_feeds_are_candidates_not_fetches(self, _resolve):
        robots = (
            b"User-agent: *\nAllow: /\n"
            b"Sitemap: /sitemap.xml\n"
            b"Sitemap: https://cdn.example.net/external.xml\n"
        )
        page = b'''<html><head><link rel="alternate" type="application/atom+xml" href="/feed.atom"></head>
            <body><main><h1>Latest report</h1><p>Release beta</p></main></body></html>'''
        with patch("oevira.acquisition._read_public", side_effect=[
            ("https://example.com/robots.txt", 200, {}, robots),
            ("https://example.com/news", 200, {"content-type": "text/html; charset=utf-8"}, page),
        ]) as fetch:
            result = acquire_url("https://example.com/news")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(
            {item["url"] for item in result["discovered_links"]},
            {"https://example.com/sitemap.xml", "https://example.com/feed.atom"},
        )
        self.assertNotIn("https://cdn.example.net/external.xml", {item["url"] for item in result["discovered_links"]})
        self.assertTrue(any("candidates only" in item for item in result["limitations"]))

    @patch("oevira.acquisition.socket.getaddrinfo", return_value=PUBLIC)
    def test_literal_authorization_and_pinned_public_host(self, _resolve):
        normalized, host, port, ip = public_target("https://example.com/report#section")
        self.assertEqual((normalized, host, port, ip), ("https://example.com/report", "example.com", 443, "93.184.215.14"))
        for url in ("file:///tmp/test", "http://user:secret@example.com/", "https://example.com:9999/", "https://example.com/\nheader", "https://[invalid/"):
            with self.assertRaises(AcquisitionError):
                public_target(url)

    def test_any_private_dns_answer_blocks(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with patch("oevira.acquisition.socket.getaddrinfo", return_value=PUBLIC + private):
            with self.assertRaises(AcquisitionError) as result:
                public_target("https://example.com/")
        self.assertEqual(result.exception.code, "PRIVATE_SOURCE_DENIED")

    @patch("oevira.acquisition.socket.getaddrinfo", return_value=PUBLIC)
    def test_capture_provenance_and_unknown_native_time(self, _resolve):
        with patch("oevira.acquisition._read_public", side_effect=[
            ("https://example.com/robots.txt", 404, {}, b""),
            ("https://example.com/report", 200, {"content-type": "text/html; charset=utf-8", "etag": '"v1"'},
             "<title>公开报告</title><script>not evidence</script><p>本期样本 32 条。</p>".encode()),
        ]):
            result = acquire_url("https://example.com/report")
        self.assertEqual(result["title"], "公开报告")
        self.assertEqual(result["text"], "本期样本 32 条。")
        self.assertIsNone(result["published_at"])
        self.assertTrue(result["captured_at"])
        self.assertIn(b"<script>", result["content"])

    @patch("oevira.acquisition.socket.getaddrinfo", return_value=PUBLIC)
    def test_denied_policy_never_fetches_content(self, _resolve):
        with patch("oevira.acquisition._read_public", return_value=("https://example.com/robots.txt", 200, {}, b"User-agent: *\nDisallow: /")) as fetch:
            with self.assertRaises(AcquisitionError) as result:
                acquire_url("https://example.com/report")
        self.assertEqual(result.exception.code, "SOURCE_POLICY_DENIED")
        self.assertEqual(fetch.call_count, 1)

    @patch("oevira.acquisition.socket.getaddrinfo", return_value=PUBLIC)
    def test_not_modified_is_not_new_observation(self, _resolve):
        with patch("oevira.acquisition._read_public", side_effect=[
            ("https://example.com/robots.txt", 404, {}, b""),
            ("https://example.com/report", 304, {}, b""),
        ]):
            result = acquire_url("https://example.com/report", etag='"v1"')
        self.assertEqual(result["status"], "unchanged")
        self.assertNotIn("text", result)

    @patch("oevira.acquisition.socket.getaddrinfo", return_value=PUBLIC)
    def test_redirect_cannot_escape_source_policy(self, _resolve):
        with patch("oevira.acquisition._PinnedHTTPS") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 302
            response.getheaders.return_value = [("Location", "/private/report")]
            with self.assertRaises(AcquisitionError) as result:
                _read_public("https://example.com/report", headers={}, deadline=time.monotonic() + 10,
                             allow_url=lambda url: "/private/" not in url)
        self.assertEqual(result.exception.code, "SOURCE_POLICY_DENIED")
        self.assertEqual(connection.return_value.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
