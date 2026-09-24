"""蜂群经验传递测试：匹配度评分 → 好坏分档 → 注入下游蜂输入。

覆盖用户要求的核心机制：
  1) 每只蜂有 0-10 匹配度（match_quality score 问题）
  2) 分档 good/neutral/poor（match_tier）
  3) 好经验传下游学习、差经验传下游避免（_build_lessons + swarm.lessons 事件）
  4) 下游蜂的 inputs 里真的收到 lesson 记录（经验传递闭环）
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import unittest
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import swarm  # noqa: E402
from swarm import match_tier, MATCH_SCORE_DEFAULT  # noqa: E402


class MatchTierTests(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(match_tier(9.0), "good")
        self.assertEqual(match_tier(6.5), "good")     # 边界含等号
        self.assertEqual(match_tier(6.4), "neutral")
        self.assertEqual(match_tier(5.0), "neutral")
        self.assertEqual(match_tier(4.1), "neutral")
        self.assertEqual(match_tier(4.0), "poor")    # 边界含等号
        self.assertEqual(match_tier(1.0), "poor")

    def test_bad_input_is_neutral(self):
        self.assertEqual(match_tier("abc"), "neutral")
        self.assertEqual(match_tier(None), "neutral")


class MatchScoreNormalizerTests(unittest.TestCase):
    """_match_score_or 的回归护栏。

    重点：不能复用 swarm_planner.coerce_risk_score —— 它把 str(None)="none" 当成
    风险标签映射成 1.0 分，会让「Jev 漏答匹配度」被误判成 poor（1 分），从而把这只
    蜂的「差经验」当真实教训传给下游蜂。缺答的正确语义是「无法评估」→ 中性 5.0。
    """

    def test_missing_and_bool_fall_back_to_neutral(self):
        self.assertEqual(swarm._match_score_or(None, MATCH_SCORE_DEFAULT), 5.0)
        self.assertEqual(swarm._match_score_or(True, MATCH_SCORE_DEFAULT), 5.0)
        self.assertEqual(swarm._match_score_or(False, MATCH_SCORE_DEFAULT), 5.0)
        self.assertEqual(swarm._match_score_or("abc", MATCH_SCORE_DEFAULT), 5.0)
        self.assertEqual(swarm._match_score_or({}, MATCH_SCORE_DEFAULT), 5.0)

    def test_numeric_answers_pass_through_and_clamp(self):
        self.assertEqual(swarm._match_score_or(8.7, 5.0), 8.7)
        self.assertEqual(swarm._match_score_or(1.5, 5.0), 1.5)
        self.assertEqual(swarm._match_score_or(15, 5.0), 10.0)   # 上限夹逼
        self.assertEqual(swarm._match_score_or(-3, 5.0), 0.0)    # 下限夹逼

    def test_dict_answers_extract_score(self):
        self.assertEqual(swarm._match_score_or({"score": 7.2}, 5.0), 7.2)
        self.assertEqual(swarm._match_score_or({"type": "score", "score": 3.0}, 5.0), 3.0)
        self.assertEqual(swarm._match_score_or({"noul": 6.8}, 5.0), 6.8)


class StageQuestionsTests(unittest.TestCase):
    def test_match_quality_question_is_score_type(self):
        qs = swarm._stage_questions(["compiler", "verifier"])
        for bee in ("compiler", "verifier"):
            q = qs[f"match_quality_{bee}"]
            self.assertEqual(q["type"], "score")
            self.assertTrue(q["instructions"])
            self.assertIsInstance(q["criteria"], list)

    def test_stage_actions_reads_score_and_tier(self):
        resp = {"ok": True, "answers": {
            "correction_action_compiler": "accept",
            "conflict_present_compiler": False,
            "match_quality_compiler": {"type": "score", "score": 8.7},
        }}
        checks = swarm._stage_actions(resp, ["compiler"])
        self.assertEqual(checks["compiler"]["match_score"], 8.7)
        self.assertEqual(checks["compiler"]["lesson_tier"], "good")

    def test_missing_score_falls_back_to_neutral_default(self):
        """Jev 没答 match_quality（或答成布尔）时回落到中性，不当成 0/1 分。"""
        resp = {"ok": True, "answers": {
            "correction_action_compiler": "accept",
            "conflict_present_compiler": False,
        }}
        checks = swarm._stage_actions(resp, ["compiler"])
        self.assertEqual(checks["compiler"]["match_score"], MATCH_SCORE_DEFAULT)
        self.assertEqual(checks["compiler"]["lesson_tier"], "neutral")

    def test_bool_answer_does_not_become_1_point(self):
        """旧 fake checker 会给所有非 choice 问题答 True；不能被当成 1.0 分。"""
        resp = {"ok": True, "answers": {
            "correction_action_compiler": "accept",
            "conflict_present_compiler": False,
            "match_quality_compiler": True,
        }}
        checks = swarm._stage_actions(resp, ["compiler"])
        self.assertGreaterEqual(checks["compiler"]["match_score"], MATCH_SCORE_DEFAULT)


class BuildLessonsTests(unittest.TestCase):
    def test_good_and_poor_collected_neutral_dropped(self):
        q = {
            "compiler":     {"match_score": 8.8, "lesson_tier": "good",
                             "summary": {"summary": "统计准确"}},
            "investigator": {"match_score": 2.1, "lesson_tier": "poor",
                             "summary": {"text": "没找到文件就下结论"}},
            "extractor":    {"match_score": 5.0, "lesson_tier": "neutral",
                             "summary": {"summary": "普通"}},
        }
        rec = swarm._build_lessons(q)
        self.assertEqual(rec["kind"], "stage_lessons")
        self.assertEqual([e["bee"] for e in rec["learn_from"]], ["compiler"])
        self.assertEqual([e["bee"] for e in rec["avoid"]], ["investigator"])
        self.assertNotIn("extractor", json.dumps(rec))
        self.assertIn("借鉴", rec["learn_guidance"])
        self.assertIn("避免", rec["avoid_guidance"])

    def test_empty_when_all_neutral(self):
        self.assertEqual(swarm._build_lessons({}), {})
        self.assertEqual(swarm._build_lessons(
            {"a": {"lesson_tier": "neutral"}}), {})

    def test_sorted_by_score_and_capped(self):
        q = {f"bee{i}": {"match_score": float(i), "lesson_tier": "good" if i >= 7 else "poor",
                         "summary": {"summary": f"s{i}"}} for i in range(10)}
        rec = swarm._build_lessons(q)
        goods = [e["match_score"] for e in rec["learn_from"]]
        self.assertEqual(goods, sorted(goods, reverse=True))
        self.assertLessEqual(len(rec["learn_from"]), 4)
        self.assertLessEqual(len(rec["avoid"]), 4)

    def test_summary_length_capped(self):
        q = {"a": {"match_score": 9, "lesson_tier": "good",
                   "summary": {"summary": "x" * 5000}}}
        rec = swarm._build_lessons(q)
        self.assertLessEqual(len(rec["learn_from"][0]["summary"]), 300)

    def test_summary_from_various_shapes(self):
        for src in ("s", {"text": "t"}, {"output": "o"}, {"status": "st"}, {"a": 1}, None):
            q = {"a": {"match_score": 9, "lesson_tier": "good", "summary": src}}
            rec = swarm._build_lessons(q)
            self.assertTrue(isinstance(rec["learn_from"][0]["summary"], str))


class LessonFlowE2ETests(unittest.IsolatedAsyncioTestCase):
    """端到端：research 配方跑一遍，验证 swarm.lessons 事件与下游 inputs 收到经验。"""

    async def test_lessons_emitted_and_delivered_downstream(self):
        captured_inputs: dict[str, list[Any]] = {}

        async def runner(bee_id, context):
            captured_inputs[bee_id] = list(context.get("inputs") or [])
            return {"summary": f"{bee_id} 完成", "stage": context["stage"]}

        # 给 compiler 高分、investigator 低分，看经验是否分档传递
        async def checker(state, questions):
            answers = {}
            for key in questions:
                if key.startswith("correction_action_"):
                    answers[key] = "accept"
                elif key.startswith("conflict_present_"):
                    answers[key] = False
                elif key.startswith("match_quality_"):
                    bee = key.rsplit("_", 1)[-1]
                    answers[key] = {"type": "score",
                                    "score": 9.0 if bee == "compiler" else
                                    (1.5 if bee == "investigator" else 5.0)}
                else:
                    answers[key] = True
            return {"ok": True, "answers": answers}

        events = []
        async for ev in swarm.orchestrate(
            "分析多个来源并给出证据", runner, checker,
            recipe="research", max_parallel=2, run_id="run_lessons",
        ):
            events.append(ev)

        types = [e.get("type") for e in events]
        self.assertEqual(types[-1], "swarm.done")
        self.assertIn("swarm.lessons", types)
        self.assertIn("bee.check", types)

        # bee.check 带 match_score / lesson_tier
        checks = [e for e in events if e["type"] == "bee.check"]
        compiler_check = next(c for c in checks if c["payload"]["bee_id"] == "compiler")
        self.assertEqual(compiler_check["payload"]["match_score"], 9.0)
        self.assertEqual(compiler_check["payload"]["lesson_tier"], "good")
        inv_check = next(c for c in checks if c["payload"]["bee_id"] == "investigator")
        self.assertEqual(inv_check["payload"]["lesson_tier"], "poor")

        # swarm.lessons 事件：每条只含**该阶段**的好/差经验（compiler 在 stage_1、
        # investigator 在 stage_2，分属不同 lessons 事件），故聚合所有事件再断言。
        lesson_evs = [e for e in events if e["type"] == "swarm.lessons"]
        self.assertTrue(lesson_evs, "至少应发出一条 swarm.lessons")
        all_learn, all_avoid = [], []
        for ev in lesson_evs:
            lessons = ev["payload"]["lessons"]
            self.assertEqual(lessons["kind"], "stage_lessons")
            all_learn += [e["bee"] for e in lessons.get("learn_from", [])]
            all_avoid += [e["bee"] for e in lessons.get("avoid", [])]
        self.assertIn("compiler", all_learn)      # 9.0 分 → good → learn_from
        self.assertIn("investigator", all_avoid)  # 1.5 分 → poor → avoid

        # 经验传递闭环：下游蜂（stage_2 及以后）的 inputs 里收到 lesson 记录
        got_lesson = False
        for bee_id, inputs in captured_inputs.items():
            for item in inputs:
                if isinstance(item, dict) and item.get("kind") == "stage_lessons":
                    got_lesson = True
                    self.assertTrue("learn_from" in item or "avoid" in item)
        self.assertTrue(got_lesson, "下游蜂必须收到经验记录")

    async def test_all_neutral_no_lessons_event(self):
        """全是 neutral 时不发 swarm.lessons（避免噪声）。"""
        async def runner(bee_id, context):
            return {"summary": f"{bee_id} ok", "stage": context["stage"]}

        async def checker(state, questions):
            answers = {}
            for key in questions:
                if key.startswith("correction_action_"):
                    answers[key] = "accept"
                elif key.startswith("conflict_present_"):
                    answers[key] = False
                elif key.startswith("match_quality_"):
                    answers[key] = {"type": "score", "score": 5.0}
                else:
                    answers[key] = True
            return {"ok": True, "answers": answers}

        types = []
        async for ev in swarm.orchestrate(
            "简单任务", runner, checker, recipe="research", run_id="run_neutral",
        ):
            types.append(ev.get("type"))
        self.assertEqual(types[-1], "swarm.done")
        self.assertNotIn("swarm.lessons", types)


if __name__ == "__main__":
    unittest.main()
