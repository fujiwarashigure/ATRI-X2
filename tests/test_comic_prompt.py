"""漫画提示词编译测试（P0-5）。

重点：

* 中文台词**逐字**进入最终提示词（不翻译、不压缩、不丢）；
* 格数/格序/人物—参考图映射是受保护块：超长时先丢风格片段，仍超长就**报错**，
  不静默丢分镜；
* 参考图编号顺序与计划一致（运行时 references 必须同序）；
* 负向提示词的三种通道与普通生图一致。
"""

from __future__ import annotations

import unittest

from atri_x.comic import (
    MAX_COMIC_REFERENCES,
    ComicCharacter,
    ComicCharacterReference,
    ComicDialogue,
    ComicPanel,
    ComicPlan,
    ComicPromptError,
    ComicSource,
    build_comic_prompt,
    layout_description,
)


def _character(index: int = 1, name: str = "小明", kind: str = "avatar") -> ComicCharacter:
    return ComicCharacter(
        id=f"member:qqonebot:{1000 + index}",
        display_name=name,
        source="avatar" if kind == "avatar" else "character",
        member_id=str(1000 + index),
        reference=ComicCharacterReference(kind=kind, source=f"https://a/{index}.png"),
    )


def _plan(panel_count: int = 2, characters=None, dialogue_texts=None) -> ComicPlan:
    cast = characters or [_character(1), _character(2, "小红")]
    texts = dialogue_texts or ["今天去吃火锅吧", "好啊，我请客"]
    panels = []
    for index in range(panel_count):
        speaker = cast[index % len(cast)]
        panels.append(
            ComicPanel(
                index=index + 1,
                scene=f"第{index + 1}格：两人在群里聊天，表情开心",
                character_ids=[speaker.id],
                dialogue=[
                    ComicDialogue(
                        speaker_id=speaker.id,
                        text=texts[index % len(texts)],
                        kind="original",
                        source_message_ids=[f"msg-{index + 1}"],
                    )
                ],
                source_message_ids=[f"msg-{index + 1}"],
            )
        )
    return ComicPlan(
        plan_id="plan-1",
        revision=1,
        title="火锅之夜",
        panel_count=panel_count,
        characters=cast,
        panels=panels,
        source=ComicSource(
            platform="qqonebot",
            scope_id="group-1",
            message_ids=[f"msg-{index + 1}" for index in range(panel_count)],
        ),
    )


class ComposeTest(unittest.TestCase):
    def test_dialogue_is_verbatim_in_prompt(self) -> None:
        plan = _plan(2, dialogue_texts=["今晚吃火锅，谁不来谁是狗", "我我我"])
        prompt = build_comic_prompt(plan, reference_characters=plan.reference_order())
        self.assertIn("今晚吃火锅，谁不来谁是狗", prompt.positive)
        self.assertIn("我我我", prompt.positive)
        self.assertIn("逐字", prompt.positive)

    def test_panel_order_is_explicit(self) -> None:
        prompt = build_comic_prompt(_plan(3), reference_characters=_plan(3).reference_order())
        for index in (1, 2, 3):
            self.assertIn(f"第{index}格", prompt.positive)

    def test_reference_numbering_matches_order(self) -> None:
        plan = _plan(2)
        prompt = build_comic_prompt(plan, reference_characters=plan.reference_order())
        self.assertIn("参考图#1 = 小明", prompt.positive)
        self.assertIn("参考图#2 = 小红", prompt.positive)
        self.assertEqual(prompt.reference_number(plan.characters[0].id), 1)
        self.assertEqual(prompt.reference_number(plan.characters[1].id), 2)
        self.assertEqual(prompt.reference_number("nope"), 0)

    def test_no_reference_still_states_it(self) -> None:
        plan = _plan(2)
        prompt = build_comic_prompt(plan, reference_characters=[])
        self.assertIn("没有可用参考图", prompt.positive)

    def test_style_is_dropped_before_protected_blocks(self) -> None:
        plan = _plan(2)
        prompt = build_comic_prompt(
            plan,
            reference_characters=[],
            global_preset="超长风格描述 " * 200,
            max_runes=1200,
        )
        self.assertTrue(prompt.dropped)
        self.assertIn("global_preset", prompt.dropped)
        self.assertIn("今天去吃火锅吧", prompt.positive)

    def test_over_limit_raises_instead_of_dropping_panels(self) -> None:
        plan = _plan(6, dialogue_texts=["很长的台词" * 40])
        with self.assertRaises(ComicPromptError) as caught:
            build_comic_prompt(
                plan,
                reference_characters=plan.reference_order(),
                max_runes=200,
            )
        self.assertIn("max_prompt_runes", str(caught.exception))

    def test_too_many_references_is_rejected(self) -> None:
        cast = [_character(index, f"人{index}") for index in range(1, 6)]
        plan = _plan(1, characters=cast)
        with self.assertRaises(ComicPromptError) as caught:
            build_comic_prompt(plan, reference_characters=plan.reference_order())
        self.assertIn(str(MAX_COMIC_REFERENCES), str(caught.exception))

    def test_negative_field_mode(self) -> None:
        prompt = build_comic_prompt(_plan(1), extra_negative="lowres, blurry")
        self.assertEqual(prompt.negative, "lowres, blurry")
        self.assertNotIn("avoid:", prompt.positive)

    def test_negative_inline_mode(self) -> None:
        prompt = build_comic_prompt(
            _plan(1), extra_negative="lowres", negative_mode="inline"
        )
        self.assertEqual(prompt.negative, "")
        self.assertIn("avoid: lowres", prompt.positive)

    def test_negative_off_mode_warns(self) -> None:
        prompt = build_comic_prompt(
            _plan(1), extra_negative="lowres", negative_mode="off"
        )
        self.assertEqual(prompt.negative, "")
        self.assertTrue(any("off" in item for item in prompt.warnings))

    def test_character_style_block_is_included_but_droppable(self) -> None:
        plan = _plan(1)
        prompt = build_comic_prompt(
            plan,
            character_presets={plan.characters[0].id: "蓝色长发，水手服"},
        )
        self.assertIn("水手服", prompt.positive)

    def test_abridged_and_invented_dialogue_are_marked(self) -> None:
        plan = _plan(2)
        plan.panels[0].dialogue[0].kind = "abridged"
        plan.panels[1].dialogue[0].kind = "invented"
        prompt = build_comic_prompt(plan, reference_characters=[])
        self.assertIn("缩写", prompt.positive)
        self.assertIn("创作台词", prompt.positive)

    def test_layout_description(self) -> None:
        self.assertIn("条漫", layout_description("strip"))
        self.assertIn("网格", layout_description("grid"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
