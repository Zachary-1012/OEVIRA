import unittest
from unittest.mock import patch

from oevira.material_parser import parse_material


class HtmlMaterialTests(unittest.TestCase):
    def test_saved_htm_is_readable_without_executing_scripts_or_fetching_links(self):
        content = '''<html><head><meta charset="utf-8"><script>secret-script</script></head><body>
        <div style="display: none !important">hidden-value</div><ix:header>hidden-xbrl</ix:header>
        <p>营业收入 769,048 千美元</p><a href="https://example.invalid">来源声明</a></body></html>'''.encode()
        with patch("socket.socket.connect", side_effect=AssertionError("Material extraction must not use the network")):
            result = parse_material("filing.htm", content)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["format"], "htm")
        self.assertIn("营业收入 769,048 千美元", result["text"])
        for removed in ("secret-script", "hidden-value", "hidden-xbrl"):
            self.assertNotIn(removed, result["text"])
        for segment in result["segments"]:
            self.assertEqual(result["text"][segment["start"]:segment["end"]], segment["text"])

    def test_declared_encoding_and_partial_main_scope_are_explicit(self):
        content = '<meta charset="gb18030"><body><main><p>用户原件中文</p></main><footer>未纳入主内容的附注</footer></body>'.encode("gb18030")
        result = parse_material("filing.html", content)
        self.assertEqual(result["status"], "partial")
        self.assertIn("用户原件中文", result["text"])
        self.assertIn("主内容", result["reason"])
        self.assertNotIn("未纳入主内容的附注", result["text"])

    def test_script_cannot_supply_a_false_charset_declaration(self):
        content = '<script>"<meta charset=gb18030>"</script><p>真实中文原文</p>'.encode()
        result = parse_material("document.htm", content)
        self.assertEqual(result["status"], "ready")
        self.assertIn("真实中文原文", result["text"])
