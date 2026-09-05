from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal

from knowflow_analytics.contracts import DatasetSpec, SemanticRelease
from knowflow_analytics.modeling.analysis_topics import scope_canonical_names
from knowflow_analytics.query.errors import SemanticParsingError

SymbolKind = Literal["metric", "dimension"]


@dataclass(frozen=True)
class ResolvedSemanticSymbol:
    kind: SymbolKind
    id: str
    name: str


class SemanticSymbolTable:
    """Release-scoped name/alias resolver.

    Names and aliases are what the LLM sees; those symbols are resolved inside
    the selected DataSet before translating to ``bizName``.  Immutable IDs stay
    the internal authority, but the LLM-visible textual S2SQL works on the
    business-name surface.
    """

    def __init__(self, *, release: SemanticRelease, dataset: DatasetSpec) -> None:
        self.dataset = dataset
        self._dataset_symbols = {
            _normalize(item) for item in (dataset.name, *dataset.aliases) if item.strip()
        }
        self._by_symbol: dict[str, tuple[ResolvedSemanticSymbol, ...]] = {}
        self._by_id: dict[str, ResolvedSemanticSymbol] = {}
        route = next(
            (
                item
                for item in release.analysis_topic_routes
                if item.dataset_id == dataset.id
            ),
            None,
        )
        scoped_names = scope_canonical_names(release, route) if route is not None else {}

        resolved_items: list[tuple[ResolvedSemanticSymbol, tuple[str, ...]]] = []
        for metric in release.metrics:
            if metric.id not in dataset.metric_ids:
                continue
            canonical_name = scoped_names.get(metric.id, metric.name)
            resolved = ResolvedSemanticSymbol(
                kind="metric", id=metric.id, name=canonical_name
            )
            self._by_id[metric.id] = resolved
            resolved_items.append((resolved, (metric.name, *metric.aliases)))
        for dimension in release.dimensions:
            if dimension.id not in dataset.dimension_ids:
                continue
            canonical_name = scoped_names.get(dimension.id, dimension.name)
            resolved = ResolvedSemanticSymbol(
                kind="dimension",
                id=dimension.id,
                name=canonical_name,
            )
            self._by_id[dimension.id] = resolved
            resolved_items.append((resolved, (dimension.name, *dimension.aliases)))

        # Canonical compiler output is the textual-S2SQL authority. Raw names
        # and aliases remain in the semantic index for Mapper recall, but a raw
        # collision must not make a root member's canonical name ambiguous at
        # translation time. Register canonical names first, then admit only raw
        # spellings that cannot name another scope member.
        pending: dict[str, dict[tuple[SymbolKind, str], ResolvedSemanticSymbol]] = {}
        canonical_owner = {
            _normalize(item.name): (item.kind, item.id) for item in self._by_id.values()
        }
        raw_owners: dict[str, set[tuple[SymbolKind, str]]] = {}
        for resolved, raw_values in resolved_items:
            for value in raw_values:
                if value.strip():
                    raw_owners.setdefault(_normalize(value), set()).add(
                        (resolved.kind, resolved.id)
                    )
        for resolved, raw_values in resolved_items:
            canonical_key = _normalize(resolved.name)
            pending.setdefault(canonical_key, {})[(resolved.kind, resolved.id)] = resolved
            for value in raw_values:
                if not value.strip():
                    continue
                key = _normalize(value)
                owner = canonical_owner.get(key)
                current = (resolved.kind, resolved.id)
                if owner is not None and owner != current:
                    continue
                if len(raw_owners.get(key, ())) > 1 and owner != current:
                    continue
                pending.setdefault(key, {})[current] = resolved
        self._by_symbol = {symbol: tuple(items.values()) for symbol, items in pending.items()}

    @classmethod
    def from_release(
        cls,
        release: SemanticRelease,
        *,
        dataset_id: str,
    ) -> SemanticSymbolTable:
        try:
            dataset = next(item for item in release.datasets if item.id == dataset_id)
        except StopIteration as exc:
            raise _invalid("selected dataset is not present in the release") from exc
        return cls(release=release, dataset=dataset)

    def require_dataset(self, value: str) -> None:
        if _normalize(value) not in self._dataset_symbols:
            raise _invalid("S2SQL dataset does not match the selected dataset")

    def is_dataset(self, value: str) -> bool:
        return _normalize(value) in self._dataset_symbols

    def resolve_first(self, value: str) -> ResolvedSemanticSymbol:
        """Resolve one scope-local symbol without first-wins ambiguity.

        The Mapper/orchestrator owns human or LLM ambiguity resolution. Once
        textual S2SQL reaches this table, a shared raw name/alias is not enough
        evidence to choose an immutable semantic ID; the parser must use the
        compiler-produced qualified canonical name instead.
        """

        candidates = self._by_symbol.get(_normalize(value), ())
        if not candidates:
            # 只说"未知名称"既不告诉用户该改成什么，也不给模型重试的线索。
            # 列出可用业务名（截断，避免大模型场景下刷屏）。
            available = sorted({item.name for item in self._by_id.values()})
            preview = "、".join(available[:12])
            if len(available) > 12:
                preview += f" 等 {len(available)} 个"
            raise _invalid(
                f"unknown semantic business name: {value}"
                + (f"；可用名称：{preview}" if preview else ""),
                # 结构化标记：并集反推时用它区分"这个作用域不拥有该成员"与
                # "这条查询自己有毛病"，不靠解析报错文案。
                details={"unknown_name": value},
            )
        if len(candidates) > 1:
            raise _invalid(
                f"ambiguous semantic business name: {value}",
                code="LLM_S2SQL_AMBIGUOUS_SYMBOL",
            )
        return candidates[0]

    def canonical_name(self, element_id: str) -> str:
        resolved = self._by_id.get(element_id)
        if resolved is None:
            raise _invalid(f"semantic element is outside the selected dataset: {element_id}")
        return resolved.name


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _invalid(
    message: str,
    *,
    code: str = "LLM_S2SQL_AST_INVALID",
    details: dict[str, object] | None = None,
) -> SemanticParsingError:
    return SemanticParsingError(message, code=code, details=details)
