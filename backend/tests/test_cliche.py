"""阶段七单元测试：去 AI 味扫描（命中位置 / 密度 / 重叠去重）。"""
import unittest

from app.application.cliche import CLICHE_TERMS, scan_cliches


class ClicheScanTest(unittest.TestCase):
    def test_hit_position_and_category(self):
        content = "他缓缓抬起头。"
        result = scan_cliches(content)
        self.assertEqual(result["total"], 1)
        hit = result["hits"][0]
        self.assertEqual(hit["term"], "缓缓")
        self.assertEqual(hit["category"], "万能副词")
        self.assertEqual(hit["start"], 1)
        self.assertEqual(hit["end"], 3)
        # 命中区间能原样切回词条本身
        self.assertEqual(content[hit["start"] : hit["end"]], "缓缓")

    def test_density_per_thousand(self):
        # 10 字命中 5 处 → 每千字 500.0
        result = scan_cliches("不禁" * 5)
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["density"], 500.0)

    def test_hits_sorted_by_start(self):
        result = scan_cliches("缓缓开口，不禁笑了。")
        starts = [h["start"] for h in result["hits"]]
        self.assertEqual(starts, sorted(starts))

    def test_overlap_dedup(self):
        # 「仿佛整个世界都」包含「仿佛」，长词先命中，短词不再重复计数
        content = "仿佛整个世界都安静了下来，仿佛一切都停止了。"
        result = scan_cliches(content)
        terms = [(h["term"], h["start"]) for h in result["hits"]]
        self.assertIn(("仿佛整个世界都", 0), terms)
        self.assertNotIn(("仿佛", 0), terms)
        # 只有长词 1 处 + 第二处独立「仿佛」1 处
        self.assertEqual(result["total"], 2)

    def test_by_category_shape(self):
        result = scan_cliches("他缓缓开口。")
        self.assertEqual(set(result["by_category"]), set(CLICHE_TERMS))
        self.assertEqual(result["by_category"]["万能副词"], 1)
        self.assertEqual(result["by_category"]["套路动作"], 0)

    def test_empty_content(self):
        result = scan_cliches("")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["density"], 0.0)


if __name__ == "__main__":
    unittest.main()
