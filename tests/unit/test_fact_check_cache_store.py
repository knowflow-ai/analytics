"""画像与核对结果的缓存读写。

两张表都只是缓存：查不到不是错误，是「这个对象还没核对过」。真正要钉住的是
①画像绑 schema 快照，换一个库就是另一份画像；②核对结果按内容键取，键变了就取不到。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.modeling.fact_checks import FactCheckKind, FactCheckRecord
from knowflow_analytics.modeling.profile import ColumnProfile, TableProfile
from knowflow_analytics.modeling.quality import QualityStatus

_SNAPSHOT = "sha256:snapshot"


@pytest.fixture()
def catalog() -> CatalogStore:
    store = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema()
    store.create_project(name="问数", project_id="proj_1")
    return store


def _profile(table: str, *, distinct: int) -> TableProfile:
    return TableProfile(
        schema_name="public",
        table=table,
        row_count=44224,
        columns=(
            ColumnProfile(
                column="zhhao",
                row_count=44224,
                non_null_count=44224,
                distinct_count=distinct,
            ),
        ),
    )


def test_column_profiles_survive_the_modeling_job(catalog: CatalogStore) -> None:
    catalog.save_table_profiles(
        project_id="proj_1",
        schema_snapshot_hash=_SNAPSHOT,
        profiles=(_profile("score_model_acct", distinct=411), _profile("acct", distinct=116)),
    )

    loaded = catalog.load_table_profiles(project_id="proj_1", schema_snapshot_hash=_SNAPSHOT)

    assert [item.table for item in loaded] == ["acct", "score_model_acct"]
    assert loaded[1].column("zhhao").distinct_ratio == pytest.approx(411 / 44224)


def test_a_re_profile_replaces_the_old_numbers(catalog: CatalogStore) -> None:
    """同一张表重新画像是覆盖，不是追加——两份统计同时在库里会让读取方选错。"""

    catalog.save_table_profiles(
        project_id="proj_1",
        schema_snapshot_hash=_SNAPSHOT,
        profiles=(_profile("score_model_acct", distinct=411),),
    )
    catalog.save_table_profiles(
        project_id="proj_1",
        schema_snapshot_hash=_SNAPSHOT,
        profiles=(_profile("score_model_acct", distinct=44224),),
    )

    loaded = catalog.load_table_profiles(project_id="proj_1", schema_snapshot_hash=_SNAPSHOT)

    assert len(loaded) == 1
    assert loaded[0].column("zhhao").distinct_count == 44224


def test_profiles_belong_to_the_database_they_were_measured_on(catalog: CatalogStore) -> None:
    catalog.save_table_profiles(
        project_id="proj_1",
        schema_snapshot_hash=_SNAPSHOT,
        profiles=(_profile("score_model_acct", distinct=411),),
    )

    assert (
        catalog.load_table_profiles(project_id="proj_1", schema_snapshot_hash="sha256:another")
        == ()
    )


def _record(subject_hash: str) -> FactCheckRecord:
    return FactCheckRecord(
        kind=FactCheckKind.GRAIN,
        subject_id="model_acct",
        subject_hash=subject_hash,
        status=QualityStatus.BLOCKING,
        payload={"duplicate_rows": 41773},
        computed_at=datetime.now(UTC),
    )


def test_a_fact_check_is_found_by_its_content_key(catalog: CatalogStore) -> None:
    catalog.save_fact_check(project_id="proj_1", record=_record("sha256:key"))

    found = catalog.load_fact_checks(project_id="proj_1", subject_hashes=("sha256:key",))

    assert found["sha256:key"].payload == {"duplicate_rows": 41773}
    assert found["sha256:key"].status is QualityStatus.BLOCKING


def test_a_changed_object_simply_finds_nothing(catalog: CatalogStore) -> None:
    """键即失效规则：对象一变键就变，旧结果查不到，不可能摆在改过的对象旁边。"""

    catalog.save_fact_check(project_id="proj_1", record=_record("sha256:before"))

    assert catalog.load_fact_checks(project_id="proj_1", subject_hashes=("sha256:after",)) == {}


def test_recomputing_the_same_object_replaces_the_stale_numbers(catalog: CatalogStore) -> None:
    catalog.save_fact_check(project_id="proj_1", record=_record("sha256:key"))
    fresh = _record("sha256:key").model_copy(
        update={"status": QualityStatus.PASSED, "payload": {"duplicate_rows": 0}}
    )
    catalog.save_fact_check(project_id="proj_1", record=fresh)

    found = catalog.load_fact_checks(project_id="proj_1", subject_hashes=("sha256:key",))

    assert len(found) == 1
    assert found["sha256:key"].status is QualityStatus.PASSED


def test_another_project_never_sees_this_evidence(catalog: CatalogStore) -> None:
    catalog.create_project(name="别的项目", project_id="proj_2")
    catalog.save_fact_check(project_id="proj_1", record=_record("sha256:key"))

    assert catalog.load_fact_checks(project_id="proj_2", subject_hashes=("sha256:key",)) == {}
