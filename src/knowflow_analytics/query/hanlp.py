from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from knowflow_analytics.semantic.index import SemanticIndexEntry

_DEFAULT_FREQUENCY = 100_000
_ASCII_WORD = re.compile(r"^[a-z0-9_$]+$")


class HanlpTerm(BaseModel):
    """Behaviorally relevant projection of HanLP ``seg.common.Term``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    word: str
    offset: int = Field(ge=0)
    length: int = Field(ge=1)
    frequency: int = Field(default=_DEFAULT_FREQUENCY, ge=1)
    natures: tuple[str, ...]


class HanlpCustomDictionary:
    """Forced custom-dictionary segmentation stage, in pure Python.

    Generic NER, place, organization and number-quantifier recognition are all
    disabled; only terms whose nature starts with the semantic custom prefix are
    kept. Consequently the query mapper consumes longest custom-dictionary hits,
    offsets, natures and frequency; the general HanLP token stream is irrelevant.
    """

    def __init__(self, entries: tuple[SemanticIndexEntry, ...]) -> None:
        by_phrase: dict[str, list[SemanticIndexEntry]] = defaultdict(list)
        original_by_phrase: dict[str, str] = {}
        for entry in entries:
            phrase = _dictionary_text(entry.phrase)
            if not phrase:
                continue
            by_phrase[phrase].append(entry)
            original_by_phrase.setdefault(phrase, entry.phrase)
        self._entries = {
            phrase: tuple(sorted(items, key=lambda item: (item.element_type, item.element_id)))
            for phrase, items in by_phrase.items()
        }
        self._originals = original_by_phrase
        self._phrases = tuple(sorted(self._entries, key=lambda item: (-len(item), item)))

    def segment(self, text: str) -> tuple[HanlpTerm, ...]:
        normalized = _dictionary_text(text)
        terms: list[HanlpTerm] = []
        offset = 0
        while offset < len(normalized):
            matched = next(
                (
                    phrase
                    for phrase in self._phrases
                    if normalized.startswith(phrase, offset)
                    and _ascii_boundary(normalized, phrase, offset)
                ),
                None,
            )
            if matched is None:
                offset += 1
                continue
            entries = self._entries[matched]
            terms.append(
                HanlpTerm(
                    word=text[offset : offset + len(matched)],
                    offset=offset,
                    length=len(matched),
                    natures=tuple(
                        sorted(
                            {
                                f"{dataset_id}:{entry.element_type.value}:{entry.element_id}"
                                for entry in entries
                                for dataset_id in entry.dataset_ids
                            }
                        )
                    ),
                )
            )
            offset += len(matched)
        return tuple(terms)


def _dictionary_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _ascii_boundary(text: str, phrase: str, offset: int) -> bool:
    if _ASCII_WORD.fullmatch(phrase) is None:
        return True
    before = text[offset - 1] if offset > 0 else ""
    after_offset = offset + len(phrase)
    after = text[after_offset] if after_offset < len(text) else ""
    return not (before and _ASCII_WORD.fullmatch(before)) and not (
        after and _ASCII_WORD.fullmatch(after)
    )
