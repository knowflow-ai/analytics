import threading
import time

from knowflow_analytics.modeling.contracts import (
    DimensionValueCandidate,
    ModelingRevision,
)
from knowflow_analytics.modeling.dimension_aliases import DimensionValueAliasSuggester


def test_alias_suggester_uses_candidate_ids_and_rejects_no_raw_value_changes(sales_release):
    class Gateway:
        def generate_json(self, **kwargs):
            assert kwargs["purpose"] == "analytics.dimension_value_aliases"
            return {
                "items": [
                    {
                        "candidate_id": "candidate-b",
                        "display_name": "展示 B",
                        "aliases": ["别称 B"],
                    },
                    {
                        "candidate_id": "candidate-a",
                        "display_name": "展示 A",
                        "aliases": ["别称 A"],
                    },
                ]
            }

    revision = ModelingRevision(
        id="revision-alias",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=1,
        semantic_spec=sales_release,
    )
    candidates = (
        DimensionValueCandidate(
            id="candidate-a",
            dimension_value_id="value-a",
            dimension_id="region",
            value="A",
            observed=True,
            display_name="A",
        ),
        DimensionValueCandidate(
            id="candidate-b",
            dimension_value_id="value-b",
            dimension_id="region",
            value="B",
            observed=True,
            display_name="B",
        ),
    )

    suggestions = DimensionValueAliasSuggester(Gateway()).suggest(
        revision=revision,
        candidates=candidates,
    )

    assert suggestions == {
        "candidate-a": {"display_name": "展示 A", "aliases": ("别称 A",)},
        "candidate-b": {"display_name": "展示 B", "aliases": ("别称 B",)},
    }


def test_alias_suggester_keeps_unrelated_dimensions_in_separate_model_requests(sales_release):
    calls = []

    class Gateway:
        def generate_json(self, **kwargs):
            calls.append(kwargs)
            candidate_id = kwargs["trace"]["candidate_ids"][0]
            return {
                "items": [
                    {
                        "candidate_id": candidate_id,
                        "display_name": candidate_id,
                        "aliases": [],
                    }
                ]
            }

    revision = ModelingRevision(
        id="revision-alias-isolation",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=1,
        semantic_spec=sales_release,
    )
    candidates = (
        DimensionValueCandidate(
            id="candidate-region",
            dimension_value_id="value-region",
            dimension_id="region",
            value="R1",
            observed=True,
            display_name="R1",
        ),
        DimensionValueCandidate(
            id="candidate-channel",
            dimension_value_id="value-channel",
            dimension_id="channel",
            value="C1",
            observed=True,
            display_name="C1",
        ),
    )

    suggestions = DimensionValueAliasSuggester(Gateway()).suggest(
        revision=revision,
        candidates=candidates,
    )

    assert suggestions == {
        "candidate-region": {"display_name": "candidate-region", "aliases": ()},
        "candidate-channel": {"display_name": "candidate-channel", "aliases": ()},
    }
    assert {call["trace"]["dimension_id"]: call["trace"]["candidate_ids"] for call in calls} == {
        "region": ["candidate-region"],
        "channel": ["candidate-channel"],
    }


def test_alias_suggester_runs_independent_dimensions_with_bounded_parallelism(sales_release):
    lock = threading.Lock()
    active = 0
    max_active = 0

    class Gateway:
        def generate_json(self, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            candidate_id = kwargs["trace"]["candidate_ids"][0]
            return {
                "items": [
                    {
                        "candidate_id": candidate_id,
                        "display_name": candidate_id,
                        "aliases": [],
                    }
                ]
            }

    revision = ModelingRevision(
        id="revision-alias-parallel",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=1,
        semantic_spec=sales_release,
    )
    candidates = tuple(
        DimensionValueCandidate(
            id=f"candidate-{index}",
            dimension_value_id=f"value-{index}",
            dimension_id=f"dimension-{index}",
            value=f"V{index}",
            observed=True,
            display_name=f"V{index}",
        )
        for index in range(6)
    )

    suggestions = DimensionValueAliasSuggester(Gateway()).suggest(
        revision=revision,
        candidates=candidates,
    )

    assert len(suggestions) == 6
    assert 2 <= max_active <= DimensionValueAliasSuggester._MAX_PARALLEL_DIMENSIONS


def test_invalid_alias_output_is_retried_before_failing_the_run(sales_release):
    """One malformed alias batch used to abort the whole one-click modeling run.
    A batch carries up to 200 values, so a single schema slip is likely; modeling
    retries the ModelSchema stage for the same reason and this stage must match."""

    from knowflow_analytics.gateways.model import ModelGatewayError

    class Gateway:
        def __init__(self) -> None:
            self.attempts: list[int] = []

        def generate_json(self, **kwargs):
            attempt = int(kwargs["trace"].get("attempt", "1"))
            self.attempts.append(attempt)
            if attempt == 1:
                raise ModelGatewayError("model gateway rejected the request")
            return {
                "items": [
                    {
                        "candidate_id": item,
                        "display_name": "展示",
                        "aliases": ["别称"],
                    }
                    for item in kwargs["trace"]["candidate_ids"]
                ]
            }

    revision = ModelingRevision(
        id="revision-alias-retry",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=1,
        semantic_spec=sales_release,
    )
    candidates = (
        DimensionValueCandidate(
            id="candidate-a",
            dimension_value_id="value-a",
            dimension_id="region",
            value="A",
            observed=True,
            display_name="A",
        ),
    )
    gateway = Gateway()

    suggestions = DimensionValueAliasSuggester(gateway).suggest(
        revision=revision,
        candidates=candidates,
    )

    assert gateway.attempts == [1, 2]
    assert suggestions["candidate-a"]["display_name"] == "展示"
