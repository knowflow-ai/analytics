"""问句里"用户说了、系统没听懂"的那些词。

反馈页要能收口，就得知道**补哪个说法**。而那个词此前谁都没匹配上，证据里查不到，
只能从问句按 span 补集反推。这段判断错了不报错，只会让预填出来的术语名不对，用户
每次都得手动改——那样"补充词典"这个动作就白做了一半。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.query.contracts import (
    MappingEvidence,
    MappingEvidenceChannel,
    MappingEvidenceMatch,
    MatchMethod,
)
from knowflow_analytics.query.service import _unmatched_phrases
from knowflow_analytics.semantic.index import SemanticElementType


def _evidence(*spans: tuple[str, tuple[int, int]]) -> MappingEvidence:
    """造一份"这些片段被精确命中过"的证据。"""

    return MappingEvidence(
        normalized_question="",
        dataset_ids=("d1",),
        config_version="v1",
        index_snapshot_id="idx1",
        embedding_model_id="emb1",
        matches=tuple(
            MappingEvidenceMatch(
                entry_id=f"e{index}",
                eligible_dataset_ids=("d1",),
                element_type=SemanticElementType.DIMENSION,
                element_id=f"dim{index}",
                phrase=text,
                normalized_phrase=text,
                detected_text=text,
                method=MatchMethod.EXACT,
                score=1.0,
                priority=300,
                channel=MappingEvidenceChannel.DICTIONARY,
                entry_source="test",
                detected_spans=(span,),
            )
            for index, (text, span) in enumerate(spans)
        ),
    )


class TestOnlyWhatNobodyMatched:
    def test_the_matched_part_is_excluded(self) -> None:
        """「各门店的业绩」里「门店」被精确命中，剩下的才是没听懂的部分。"""

        phrases = _unmatched_phrases("各门店的业绩", _evidence(("门店", (1, 3))))

        assert "业绩" in phrases
        assert all("门店" not in item for item in phrases)

    def test_a_fully_understood_question_leaves_nothing_worth_recording(self) -> None:
        """「各门店的销售金额」两个词都命中了，不该记任何待补说法。

        把每次成功都记下来会把真正需要补词典的说法淹掉。
        """

        phrases = _unmatched_phrases(
            "各门店的销售金额", _evidence(("门店", (1, 3)), ("销售金额", (4, 8)))
        )

        assert phrases == ()

    def test_nothing_matched_keeps_the_whole_question(self) -> None:
        assert _unmatched_phrases("哪家店最赚钱", None) == ("哪家店最赚钱",)


class TestTrimmingIsConservative:
    def test_leading_particles_are_trimmed(self) -> None:
        """切出来的是「的业绩」，预填进术语表单用户还得手动删一遍。"""

        assert _unmatched_phrases("各门店的业绩", _evidence(("门店", (1, 3)))) == ("业绩",)

    def test_a_term_containing_a_particle_is_left_alone(self) -> None:
        """只裁首尾。中间的字一律不动——不能把「客户满意度」裁成「客户满意」。"""

        assert _unmatched_phrases("客户满意度", None) == ("客户满意度",)

    def test_punctuation_splits_instead_of_gluing(self) -> None:
        """标点不属于任何说法，留着会把两侧粘成一个莫名其妙的"术语"。"""

        phrases = _unmatched_phrases("业绩，毛利", None)

        assert set(phrases) == {"业绩", "毛利"}

    @pytest.mark.parametrize(
        "question",
        [pytest.param("的", id="单字虚词"), pytest.param("", id="空问句")],
    )
    def test_nothing_worth_keeping_yields_nothing(self, question: str) -> None:
        assert _unmatched_phrases(question, None) == ()


def test_the_same_phrase_is_not_repeated() -> None:
    """同一个片段出现两次只记一条——它是聚合的键，重复会让计数虚高。"""

    assert _unmatched_phrases("业绩，业绩", None) == ("业绩",)
