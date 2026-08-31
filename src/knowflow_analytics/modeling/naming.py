from __future__ import annotations

import re

_TRAILING_UNIT = re.compile(r"\s*[（(][^）)]*[）)]\s*$")


def deterministic_field_aliases(column_name: str) -> tuple[str, ...]:
    """Return reviewable lexical aliases derived only from physical metadata.

    These are offline modeling candidates, not online query rewrites. The rules are
    intentionally small and auditable: remove only a trailing unit annotation.
    Business-language aliases must come from reviewed model suggestions instead of
    suffix rules embedded in runtime code.
    """

    normalized = column_name.strip()
    base = _TRAILING_UNIT.sub("", normalized).strip() or normalized
    aliases: list[str] = []
    if base != normalized:
        aliases.append(base)
    return tuple(dict.fromkeys(aliases))
