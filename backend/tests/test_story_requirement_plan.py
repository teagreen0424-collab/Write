"""作者原文核对计划：固定编号、动作隔离、引用与元要求范围。"""

import json
import unittest
from pathlib import Path

from app.application.story_review import build_requirement_plan


class StoryRequirementPlanTest(unittest.TestCase):
    def test_real_directive_two_actions_have_stable_separate_ids_and_shared_context(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/story_review_real_cases.json").read_text(encoding="utf-8"))
        plan = build_requirement_plan(fixture["directive"])
        self.assertEqual(plan, build_requirement_plan(fixture["directive"]))
        self.assertEqual([item.id for item in plan], [f"d{i:03}" for i in range(1, 8)])
        self.assertIn("有人建议林舟偷取原始档案", plan[0].text)
        self.assertEqual(plan[1].text, "林舟拒绝")
        self.assertEqual(plan[0].context, plan[1].context)
        self.assertIn("有人建议林舟偷取原始档案", plan[1].context)
        self.assertTrue(plan[0].evidence_required)
        self.assertTrue(plan[1].evidence_required)

    def test_quoted_punctuation_and_parenthetical_conditions_do_not_split(self):
        plan = build_requirement_plan("主角说‘别拿原件，我拒绝；要登记’（除非工作人员授权，才复印），同伴赞同。")
        self.assertEqual(len(plan), 2)
        self.assertIn("别拿原件，我拒绝；要登记", plan[0].text)
        self.assertIn("除非工作人员授权，才复印", plan[0].text)
        self.assertEqual(plan[1].text, "同伴赞同")
        self.assertEqual(plan[0].context, plan[1].context)

    def test_semicolon_retains_earlier_condition_in_shared_sentence_context(self):
        plan = build_requirement_plan("若发现有人跟踪：先确认距离；再通知同伴，不独自追赶。")
        self.assertEqual(len(plan), 3)
        self.assertIn("若发现有人跟踪", plan[2].context)
        self.assertEqual(plan[2].text, "不独自追赶")

    def test_english_quotes_and_apostrophes_preserve_their_intended_boundaries(self):
        plan = build_requirement_plan("主角说'我先走，不等他';Don't rename the town, preserve its name.")
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[0].text, "主角说'我先走，不等他'")
        self.assertEqual(plan[1].text, "Don't rename the town")

    def test_numbers_with_thousands_comma_are_not_broken_into_actions(self):
        plan = build_requirement_plan("预算1,200元，先付定金。", "正文约1,200字。")
        self.assertEqual(plan[0].text, "预算1,200元")
        self.assertEqual(plan[1].text, "先付定金")
        self.assertEqual(plan[2].id, "o001")
        self.assertFalse(plan[2].evidence_required)

    def test_scope_only_is_omitted_but_scope_with_story_constraint_is_retained(self):
        plan = build_requirement_plan("只影响接下来这一章；不改变人物伤情。只影响本章的伤情描写。")
        self.assertEqual(len(plan), 2)
        self.assertTrue(all(item.evidence_required for item in plan))
        self.assertIn("只影响接下来这一章", plan[0].context)
        self.assertEqual(plan[1].text, "只影响本章的伤情描写")

    def test_output_metadata_needs_no_scene_quote_but_actual_actions_do(self):
        plan = build_requirement_plan("", "每章正文约1200至1600个中文字符（含标点，不计空白），直接写场景、动作与对话，不列小节，不作章节总结。主角拒绝承重，不改变旧伤。")
        self.assertEqual([item.evidence_required for item in plan], [False, False, False, False, True, True])
        self.assertEqual(len({item.id for item in plan}), len(plan))
        self.assertTrue(all(item.source == "outline" for item in plan))

    def test_negative_narrative_constraints_cannot_freely_skip_evidence(self):
        plan = build_requirement_plan("不要忘记写主角拒绝的场面，不新增人物能力，不改变左手旧伤。")
        self.assertTrue(all(item.evidence_required for item in plan))

    def test_duplicate_author_text_still_has_distinct_numbered_requirements(self):
        plan = build_requirement_plan("先解释原因，再解释原因。先解释原因。")
        self.assertEqual(len(plan), 3)
        self.assertEqual([item.id for item in plan], ["d001", "d002", "d003"])

    def test_numbers_inside_narrative_are_not_misclassified_as_output_metadata(self):
        plan = build_requirement_plan("主角找到1200字的旧信，本章要写主角拒绝报价1600元。")
        self.assertTrue(all(item.evidence_required for item in plan))


if __name__ == "__main__":
    unittest.main()
