from pathlib import Path
import base64
import hashlib
from pathlib import Path
import re
import tempfile
import unittest
from oevira import create_app
from oevira.auth import issue_local_capability
from oevira.html_delivery import render_html


class HtmlDeliveryTests(unittest.TestCase):
    def test_untrusted_content_is_text_and_only_fixed_script_can_execute(self):
        content=render_html('<script>alert(1)</script>', '## 数据\n| 项目 | 预算 |\n|---|---|\n| A | 100 |\n| B | 200 |\n\n<script>fetch("/api/session")</script>', ['- 用户原话 <img src=x onerror=alert(1)>']).decode()
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',content)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;',content)
        scripts=re.findall(r'<script>(.*?)</script>',content,re.S)
        self.assertEqual(len(scripts),1)
        digest=base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
        self.assertIn('sha256-'+digest,content)
        self.assertIn("connect-src &#x27;none&#x27;",content)
        self.assertIn('>200</td>',content)
        self.assertEqual(content.count('<tbody>'),1)
        self.assertNotIn('src="http',content)

    def test_offline_browser_runs_typed_sorting_filters_charts_and_source_navigation(self):
        body = """# Revenue movement
Confirmed values use the saved source [^1].

| Period | Revenue | Note |
|---|---:|---|
| 2026-02-15 | 900719925474099312345678.4 | latest |
| 2025-12-01 | 2 | earlier |
| 2026-01-31 | 10 | middle |
"""
        content = render_html("Revenue report", body, ["- Customer sheet — actuals, cell B2"])
        html = content.decode("utf-8")
        self.assertIn('data-sort-kind="date"', html)
        self.assertIn('data-sort-kind="number"', html)
        self.assertIn('id="source-1"', html)
        self.assertIn('data-chart-row="html-table-1-row-1"', html)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("The installed Playwright Chromium runtime is unavailable.")
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                reduced_motion="reduce",
                service_workers="block",
            )
            page = context.new_page()
            requests = []
            page.on("request", lambda request: requests.append(request.url))
            page.set_content(html, wait_until="load")

            table = page.locator("table")
            period = table.locator("thead button").nth(0)
            period.click()
            observed_dates = table.locator("tbody tr td:nth-child(1)").all_text_contents()
            self.assertEqual(observed_dates, ["2025-12-01", "2026-01-31", "2026-02-15"])

            revenue = table.locator("thead button").nth(1)
            revenue.click()
            observed_values = table.locator("tbody tr td:nth-child(2)").all_text_contents()
            self.assertEqual(observed_values, ["2", "10", "900719925474099312345678.4"])
            revenue.click()
            self.assertEqual(
                table.locator("tbody tr td:nth-child(2)").all_text_contents(),
                ["900719925474099312345678.4", "10", "2"],
            )

            section = page.locator("main > section").first
            section_toggle = section.locator("summary").first
            section_toggle.click()
            self.assertFalse(section.locator("details.section-details").evaluate("node => node.open"))
            page.locator("#find").fill("Revenue movement")
            self.assertTrue(section.locator("details.section-details").evaluate("node => node.open"))
            self.assertFalse(section.locator("#html-table-1-row-2").evaluate("node => node.hidden"))

            page.locator("#find").fill("earlier")
            visible_rows = table.locator("tbody tr:not([hidden]) td:nth-child(3)").all_text_contents()
            self.assertEqual(visible_rows, ["earlier"])
            page.locator("#clear").click()

            page.locator(".source-ref").click()
            self.assertTrue(page.locator("#sources").evaluate("node => node.open"))
            self.assertEqual(page.evaluate("window.location.hash"), "#source-1")

            page.locator(".chart-details > summary").first.click()
            page.locator("[data-chart-row='html-table-1-row-1']").click()
            self.assertTrue(page.locator("#html-table-1-row-1").evaluate("node => node.classList.contains('chart-target')"))
            self.assertEqual(requests, [])
            context.close()
            browser.close()

    def test_real_saved_version_exports_replays_and_reopens(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=create_app(root,expected_port=8317);client=app.test_client()
            store=app.extensions['oevira.store'];cap=root/'launch.capability';issue_local_capability(store,cap)
            base='http://127.0.0.1:8317'
            session=client.post('/api/local-session',base_url=base,json={'capability':cap.read_text()})
            headers={'Authorization':'Bearer '+session.json['data']['token']}
            body='## 经营观察\n| 项目 | 预算 |\n|---|---|\n| A | 100 |\n| B | 200 |'
            artifact=client.post('/api/artifacts',base_url=base,headers=headers,json={'title':'报告','body':body,'citations':[]})
            self.assertEqual(artifact.status_code,201)
            aid=artifact.json['data']['id'];request={'version':1,'format':'html'}
            headers['Idempotency-Key']='html-original-request'
            exported=client.post(f'/api/artifacts/{aid}/exports',base_url=base,headers=headers,json=request)
            self.assertEqual(exported.status_code,201,exported.get_data(as_text=True))
            replay=client.post(f'/api/artifacts/{aid}/exports',base_url=base,headers=headers,json=request)
            self.assertEqual(replay.json['data']['id'],exported.json['data']['id'])
            restarted=create_app(root,expected_port=8317).test_client()
            response=restarted.get(exported.json['data']['download_url'],base_url=base,headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.data,render_html('报告',body,[]))
            self.assertEqual(hashlib.sha256(response.data).hexdigest(),exported.json['data']['sha256'])
            response.close()
