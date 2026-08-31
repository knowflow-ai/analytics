"""A semantic index id must not depend on non-reproducible embedding floats.

Reviewed contract: the snapshot id identifies *what was indexed with which
embedding model*, never the exact vectors one call happened to return.  Remote
embedding services are not bit-reproducible, and confirmation memories are
bound to the snapshot id, so hashing the vectors made every rebuild of an
unchanged release silently invalidate every stored confirmation.
"""

from __future__ import annotations

import itertools

from knowflow_analytics.semantic.index import EmbeddingBatch, SemanticIndexBuilder


class _DriftingEmbeddingGateway:
    """Return float-noise-different vectors on every call, like a real service."""

    def __init__(self) -> None:
        self._calls = itertools.count()

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        drift = next(self._calls) * 1e-9
        return EmbeddingBatch(
            model_id="drift-test",
            dimension=2,
            vectors=tuple((1.0 + drift, -0.5 - drift) for _ in texts),
        )


class _ConstantEmbeddingGateway:
    def __init__(self, *, model_id: str = "constant-test", dimension: int = 2) -> None:
        self._model_id = model_id
        self._dimension = dimension

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id=self._model_id,
            dimension=self._dimension,
            vectors=tuple((1.0,) * self._dimension for _ in texts),
        )


def test_rebuilding_an_unchanged_release_keeps_the_same_index_id(sales_release):
    builder = SemanticIndexBuilder(_DriftingEmbeddingGateway())

    first = builder.build(sales_release)
    second = builder.build(sales_release)

    assert first.vectors != second.vectors, "fixture must actually drift"
    assert first.id == second.id
    assert first.content_hash == second.content_hash


def test_a_changed_release_still_gets_its_own_index_id(sales_release):
    builder = SemanticIndexBuilder(_ConstantEmbeddingGateway())
    other = sales_release.model_copy(update={"spec_hash": "another-release"})

    assert builder.build(sales_release).id != builder.build(other).id


def test_a_changed_embedding_model_still_gets_its_own_index_id(sales_release):
    original = SemanticIndexBuilder(_ConstantEmbeddingGateway(model_id="model-a"))
    replacement = SemanticIndexBuilder(_ConstantEmbeddingGateway(model_id="model-b"))

    assert original.build(sales_release).id != replacement.build(sales_release).id


def test_a_changed_vector_dimension_still_gets_its_own_index_id(sales_release):
    narrow = SemanticIndexBuilder(_ConstantEmbeddingGateway(dimension=2))
    wide = SemanticIndexBuilder(_ConstantEmbeddingGateway(dimension=3))

    assert narrow.build(sales_release).id != wide.build(sales_release).id
