from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from knowflow_analytics.contracts import Cardinality, RelationSpec
from knowflow_analytics.errors import TranslationError


@dataclass(frozen=True)
class PlannedRelation:
    relation: RelationSpec
    from_model_id: str
    to_model_id: str


class JoinPlanner:
    """Find a unique relation tree and reject metric fanout before SQL generation."""

    def __init__(self, relations: tuple[RelationSpec, ...]) -> None:
        self._relations = {item.id: item for item in relations}
        self._adjacency: dict[str, list[tuple[str, RelationSpec]]] = defaultdict(list)
        for relation in relations:
            self._adjacency[relation.left_model_id].append((relation.right_model_id, relation))
            self._adjacency[relation.right_model_id].append((relation.left_model_id, relation))
        for edges in self._adjacency.values():
            edges.sort(key=lambda edge: edge[1].id)

    def plan(
        self,
        *,
        anchor_model_id: str,
        required_model_ids: set[str],
        has_metrics: bool,
        fanout_safe: bool = False,
    ) -> tuple[PlannedRelation, ...]:
        if required_model_ids == {anchor_model_id}:
            return ()
        selected: dict[str, PlannedRelation] = {}
        for target in sorted(required_model_ids - {anchor_model_id}):
            path = self._unique_shortest_path(anchor_model_id, target)
            current = anchor_model_id
            for next_model, relation in path:
                planned = PlannedRelation(
                    relation=relation,
                    from_model_id=current,
                    to_model_id=next_model,
                )
                existing = selected.get(relation.id)
                if existing is not None and existing != planned:
                    raise TranslationError(
                        "relation tree requires incompatible traversal",
                        code="AMBIGUOUS_JOIN_PATH",
                    )
                selected[relation.id] = planned
                current = next_model

        ordered = self._order_tree(anchor_model_id, selected)
        if has_metrics and not fanout_safe:
            for edge in ordered:
                if self._expands_rows(edge):
                    raise TranslationError(
                        f"relation {edge.relation.id} expands the metric grain",
                        code="FANOUT_RISK",
                    )
        return tuple(ordered)

    def plan_explicit(
        self,
        *,
        anchor_model_id: str,
        relation_ids: tuple[str, ...],
        required_model_ids: set[str],
        has_metrics: bool,
        fanout_safe: bool = False,
    ) -> tuple[PlannedRelation, ...]:
        """Bind a reviewed root-relative route without shortest-path discovery."""

        unknown = set(relation_ids) - set(self._relations)
        if unknown:
            raise TranslationError(
                f"analysis topic route references unknown relations: {sorted(unknown)}",
                code="ANALYSIS_TOPIC_RELATION_NOT_FOUND",
            )
        selected = {
            relation_id: PlannedRelation(
                relation=self._relations[relation_id],
                from_model_id=self._relations[relation_id].left_model_id,
                to_model_id=self._relations[relation_id].right_model_id,
            )
            for relation_id in relation_ids
        }
        ordered = self._order_tree(anchor_model_id, selected)
        reached = {anchor_model_id}
        for edge in ordered:
            reached.add(edge.from_model_id)
            reached.add(edge.to_model_id)
        if not required_model_ids.issubset(reached):
            raise TranslationError(
                "analysis topic route does not reach every requested model",
                code="ANALYSIS_TOPIC_PATH_MISSING",
            )
        if has_metrics and not fanout_safe:
            for edge in ordered:
                if self._expands_rows(edge):
                    raise TranslationError(
                        f"relation {edge.relation.id} expands the metric grain",
                        code="FANOUT_RISK",
                    )
        return tuple(ordered)

    def _unique_shortest_path(self, source: str, target: str) -> list[tuple[str, RelationSpec]]:
        queue: deque[str] = deque([source])
        distance = {source: 0}
        path_count = {source: 1}
        predecessor: dict[str, tuple[str, RelationSpec]] = {}

        while queue:
            current = queue.popleft()
            for neighbor, relation in self._adjacency.get(current, []):
                candidate_distance = distance[current] + 1
                if neighbor not in distance:
                    distance[neighbor] = candidate_distance
                    path_count[neighbor] = path_count[current]
                    predecessor[neighbor] = (current, relation)
                    queue.append(neighbor)
                elif distance[neighbor] == candidate_distance:
                    path_count[neighbor] += path_count[current]

        if target not in distance:
            raise TranslationError(
                f"no relation path from {source} to {target}",
                code="MISSING_JOIN_PATH",
            )
        if path_count[target] != 1:
            raise TranslationError(
                f"multiple relation paths from {source} to {target}",
                code="AMBIGUOUS_JOIN_PATH",
            )

        reverse_path: list[tuple[str, RelationSpec]] = []
        current = target
        while current != source:
            previous, relation = predecessor[current]
            reverse_path.append((current, relation))
            current = previous
        reverse_path.reverse()
        return reverse_path

    @staticmethod
    def _order_tree(anchor: str, selected: dict[str, PlannedRelation]) -> list[PlannedRelation]:
        remaining = dict(selected)
        visited = {anchor}
        ordered: list[PlannedRelation] = []
        while remaining:
            progress = False
            for relation_id in sorted(tuple(remaining)):
                edge = remaining[relation_id]
                left = edge.relation.left_model_id
                right = edge.relation.right_model_id
                if left in visited and right not in visited:
                    ordered.append(PlannedRelation(edge.relation, left, right))
                    visited.add(right)
                elif right in visited and left not in visited:
                    ordered.append(PlannedRelation(edge.relation, right, left))
                    visited.add(left)
                else:
                    continue
                remaining.pop(relation_id)
                progress = True
            if not progress:
                raise TranslationError(
                    "selected relations do not form a connected tree",
                    code="AMBIGUOUS_JOIN_PATH",
                )
        return ordered

    @staticmethod
    def _expands_rows(edge: PlannedRelation) -> bool:
        cardinality = edge.relation.cardinality
        if cardinality is Cardinality.MANY_TO_MANY:
            return True
        from_is_left = edge.from_model_id == edge.relation.left_model_id
        if cardinality is Cardinality.ONE_TO_MANY:
            return from_is_left
        if cardinality is Cardinality.MANY_TO_ONE:
            return not from_is_left
        return False
