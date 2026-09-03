"""按项目解析数据源，并把连接相关的组件装配出来。

在此之前，执行器、内省器、三个画像器都是在进程启动时**按唯一一个环境变量**建好的，
整个服务只能连一个库。数据源变成实体之后，这些组件必须跟着项目走。

这个模块只做一件事：``for_project(project_id)`` → 一套接好线的组件。查询链路上的
代码不需要知道数据源是怎么存的、连接串怎么解密、引擎怎么缓存。

三条设计约束：

1. **没绑数据源的项目回落到进程级默认数据源。** 存量项目一个都没有绑定行，回落让
   它们继续照常工作；不回落的话，这次升级会让所有已有项目当场问不了数。

2. **引擎按 (数据源 id, 密文) 缓存。** 只按 id 缓存的话，用户改了连接串还会连到旧
   地址，且要到进程重启才恢复；把密文放进键里，改连接串自然换引擎。

3. **解不开的连接串直接失败，绝不回落到默认数据源。** 回落意味着"本该问 A 库的
   问题悄悄问了 B 库"——数字看起来完全正常。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine

from knowflow_analytics.catalog.secrets import DataSourceSecretBox
from knowflow_analytics.catalog.store import CatalogStore, DataSourceRecord
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.execution.dialect import SqlDialect
from knowflow_analytics.execution.executor import SqlExecutor
from knowflow_analytics.execution.guard import PhysicalSqlGuard
from knowflow_analytics.modeling.introspector import SchemaIntrospector
from knowflow_analytics.modeling.profile import ColumnStatisticsProfiler
from knowflow_analytics.modeling.profiler import DimensionValueProfiler
from knowflow_analytics.modeling.quality import ModelingQualityProfiler

__all__ = [
    "DataSourceBinding",
    "DataSourceError",
    "DataSourceRegistry",
    "SingleDataSourceRegistry",
]


# 迁移出来的那个数据源用固定 id：每次启动都要能认出"已经迁过了"。
_DEFAULT_DATA_SOURCE_ID = "ds_deployment_default"


class DataSourceError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "DATA_SOURCE_UNAVAILABLE") -> None:
        super().__init__(message, code=code, stage="PRECHECK")


@dataclass(frozen=True)
class DataSourceBinding:
    """一个项目实际连的东西，以及连它需要的全部组件。"""

    data_source_id: str | None
    dialect: SqlDialect
    # 只用于关闭连接池。单数据源装配下组件是外面传进来的（测试替身也在其中），
    # 未必握着引擎，所以可以没有。
    engine: Engine | None
    executor: SqlExecutor
    introspector: SchemaIntrospector
    column_profiler: ColumnStatisticsProfiler
    semantic_profiler: DimensionValueProfiler
    quality_profiler: ModelingQualityProfiler


class DataSourceRegistry:
    def __init__(
        self,
        *,
        catalog: CatalogStore,
        secret_box: DataSourceSecretBox,
        default_database_url: str,
        default_dialect: SqlDialect = SqlDialect.POSTGRES,
        modeling_sample_values: bool = True,
        statement_timeout_ms: int = 30_000,
        lock_timeout_ms: int = 2_000,
    ) -> None:
        self._catalog = catalog
        self._secret_box = secret_box
        self._default_database_url = default_database_url
        self._default_dialect = default_dialect
        self._modeling_sample_values = modeling_sample_values
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        self._lock = threading.Lock()
        self._bindings: dict[tuple[str | None, str], DataSourceBinding] = {}

    def for_project(self, project_id: str) -> DataSourceBinding:
        data_source_id = self._catalog.get_project_data_source_id(project_id)
        if data_source_id is None:
            # **没有回落。** 曾经有过：存量项目一个绑定行都没有，回落让它们继续
            # 工作。但那是一次迁移的活，不该做成一条永久的代码路径——回落意味着
            # 项目说不出自己连的是哪个库，而这正是最容易出静默错答的地方。
            #
            # 现在由 ensure_default_data_source() 在启动时把部署配置的那个库变成
            # 一个真实的数据源记录并绑上去，所以"未绑定"只可能是被手工解绑或
            # 迁移没跑成，两种都该报出来。
            raise DataSourceError(
                "this project is not bound to a data source",
                code="DATA_SOURCE_NOT_BOUND",
            )

        record = self._catalog.get_data_source(data_source_id)
        secret = self._catalog.read_data_source_dsn(data_source_id)
        if record is None or secret is None:
            # 绑定还在、数据源没了。**不回落**：回落会让本该问 A 库的问题悄悄问了
            # B 库，数字看起来完全正常。
            raise DataSourceError(
                "the data source bound to this project no longer exists",
                code="DATA_SOURCE_NOT_FOUND",
            )
        try:
            dialect = SqlDialect(record.engine)
        except ValueError as exc:
            raise DataSourceError(
                f"unsupported data source engine: {record.engine}",
                code="DATA_SOURCE_ENGINE_UNSUPPORTED",
            ) from exc
        return self._binding(data_source_id, secret, dialect, encrypted=True)

    def _binding(
        self,
        data_source_id: str | None,
        secret_or_url: str,
        dialect: SqlDialect,
        *,
        encrypted: bool = False,
    ) -> DataSourceBinding:
        # 键里带上密文：改了连接串就自然换一套引擎，不必等进程重启。
        key = (data_source_id, secret_or_url)
        with self._lock:
            cached = self._bindings.get(key)
            if cached is not None:
                return cached

        database_url = self._secret_box.decrypt(secret_or_url) if encrypted else secret_or_url
        engine = create_engine(database_url, pool_pre_ping=True)
        executor = SqlExecutor(
            database_url,
            dialect=dialect,
            statement_timeout_ms=self._statement_timeout_ms,
            lock_timeout_ms=self._lock_timeout_ms,
            guard=PhysicalSqlGuard(dialect=dialect),
            engine=engine,
        )
        binding = DataSourceBinding(
            data_source_id=data_source_id,
            dialect=dialect,
            engine=engine,
            executor=executor,
            introspector=SchemaIntrospector(engine, dialect=dialect),
            column_profiler=ColumnStatisticsProfiler(
                engine, sample_values=self._modeling_sample_values, dialect=dialect
            ),
            semantic_profiler=DimensionValueProfiler(engine, dialect=dialect),
            quality_profiler=ModelingQualityProfiler(engine, executor, dialect=dialect),
        )
        with self._lock:
            # 并发时可能已经有人建好了；用先到的那个，把自己刚建的引擎丢掉，
            # 避免同一个数据源留下两个连接池。
            existing = self._bindings.get(key)
            if existing is not None:
                engine.dispose()
                return existing
            self._bindings[key] = binding
        return binding

    # ---- 管理 ---------------------------------------------------------------
    #
    # 放在这里而不是 application：加密、连接、缓存失效都在本模块，凭据不必再多走
    # 一层。上层只看见"名字 + 引擎 + 连不连得上"。

    def list(self) -> tuple[DataSourceRecord, ...]:
        return self._catalog.list_data_sources()

    def get(self, data_source_id: str) -> DataSourceRecord | None:
        return self._catalog.get_data_source(data_source_id)

    def create(self, *, name: str, engine: str, dsn: str) -> DataSourceRecord:
        dialect = self._parse_engine(engine)
        self.test(engine=dialect, dsn=dsn)
        return self._catalog.create_data_source(
            name=name, engine=dialect.value, secret=self._secret_box.encrypt(dsn)
        )

    def update(
        self, *, data_source_id: str, name: str | None = None, dsn: str | None = None
    ) -> DataSourceRecord | None:
        record = self._catalog.get_data_source(data_source_id)
        if record is None:
            return None
        secret = None
        if dsn is not None:
            self.test(engine=SqlDialect(record.engine), dsn=dsn)
            secret = self._secret_box.encrypt(dsn)
        updated = self._catalog.update_data_source(
            data_source_id=data_source_id, name=name, secret=secret
        )
        # 换了连接串就得丢掉旧连接池，否则它还会继续用旧地址服务到进程重启。
        self.invalidate(data_source_id)
        return updated

    def delete(self, data_source_id: str) -> bool:
        """删除。**还有项目在用就拒绝。**

        直接删掉的话，那些项目要到下一次提问时才发现数据源没了——报错点离原因很远，
        看起来像是问数坏了。让用户先解绑，他才知道自己在影响谁。
        """

        in_use = self._catalog.list_projects_using_data_source(data_source_id)
        if in_use:
            raise DataSourceError(
                f"{len(in_use)} project(s) still use this data source",
                code="DATA_SOURCE_IN_USE",
            )
        self.invalidate(data_source_id)
        return self._catalog.delete_data_source(data_source_id)

    def bind(self, *, project_id: str, data_source_id: str) -> None:
        if self._catalog.get_data_source(data_source_id) is None:
            raise DataSourceError("data source was not found", code="DATA_SOURCE_NOT_FOUND")
        self._catalog.bind_project_data_source(project_id=project_id, data_source_id=data_source_id)

    def project_data_source_id(self, project_id: str) -> str | None:
        return self._catalog.get_project_data_source_id(project_id)

    def test(self, *, engine: SqlDialect | str, dsn: str) -> None:
        """连一下，确认这套信息真的能用。

        存下去之前先试，是为了不让"填错了"变成"以后每次提问都失败"。

        **异常信息里不能带连接串。** SQLAlchemy 的报错默认会把 URL 带上，里面就有
        密码；那条消息会一路走到浏览器。所以这里只保留驱动给出的原因，自己另写
        一句。
        """

        dialect = self._parse_engine(engine)
        probe = create_engine(dsn, pool_pre_ping=False)
        try:
            with probe.connect() as connection:
                for statement in dialect.read_only_session_sql(
                    statement_timeout_ms=self._statement_timeout_ms,
                    lock_timeout_ms=self._lock_timeout_ms,
                ):
                    connection.exec_driver_sql(statement)
                connection.exec_driver_sql("SELECT 1")
        except Exception as exc:  # noqa: BLE001 - 驱动异常类型众多，一律收敛
            raise DataSourceError(
                f"could not connect to the data source: {type(exc).__name__}",
                code="DATA_SOURCE_UNREACHABLE",
            ) from None
        finally:
            probe.dispose()

    @staticmethod
    def _parse_engine(engine: SqlDialect | str) -> SqlDialect:
        if isinstance(engine, SqlDialect):
            return engine
        try:
            return SqlDialect(engine)
        except ValueError as exc:
            raise DataSourceError(
                f"unsupported data source engine: {engine}",
                code="DATA_SOURCE_ENGINE_UNSUPPORTED",
            ) from exc

    def ensure_default_data_source(self, *, name: str = "默认数据源") -> str | None:
        """把部署配置的那个库变成一个真实的数据源，并绑上所有还没绑的项目。

        这是**一次性迁移**，不是运行时回落。数据源成为实体之前，所有项目都连着
        ``datasource_database_url`` 那一个库；升级后它们没有绑定行，而
        ``for_project`` 已经不再回落。不迁移的话，升级当天所有存量项目一起报
        "没绑数据源"。

        迁移之后 UI 里也不再需要「默认库（部署配置）」这个魔法选项——它就是列表里
        一个普通数据源，可以改名、可以被别的项目复用。

        幂等：固定 id，已存在就不重建；只绑还没绑的项目。每次启动都跑，所以中途
        失败下次会补上。
        """

        if not self._default_database_url.strip():
            return None
        record = self._catalog.get_data_source(_DEFAULT_DATA_SOURCE_ID)
        if record is None:
            self._catalog.create_data_source(
                name=name,
                engine=self._default_dialect.value,
                secret=self._secret_box.encrypt(self._default_database_url),
                data_source_id=_DEFAULT_DATA_SOURCE_ID,
            )
        for project_id in self._catalog.list_unbound_project_ids():
            self._catalog.bind_project_data_source(
                project_id=project_id, data_source_id=_DEFAULT_DATA_SOURCE_ID
            )
        return _DEFAULT_DATA_SOURCE_ID

    def invalidate(self, data_source_id: str) -> None:
        """丢掉某个数据源已缓存的连接。

        改连接串后立刻调用，否则旧连接池还会继续用旧地址服务，直到进程重启。
        """

        with self._lock:
            stale = [key for key in self._bindings if key[0] == data_source_id]
            for key in stale:
                binding = self._bindings.pop(key)
                if binding.engine is not None:
                    binding.engine.dispose()

    def close(self) -> None:
        with self._lock:
            for binding in self._bindings.values():
                if binding.engine is not None:
                    binding.engine.dispose()
            self._bindings.clear()


class SingleDataSourceRegistry:
    """整个服务只连一个库时的装配。

    数据源变成实体之前就是这个形态，OSS 单库部署和绝大多数测试也仍然是。给它与
    多数据源同形的外壳，调用方就只需要认识 ``for_project`` 一种取法——不必到处
    写"有没有配数据源"的分支。
    """

    def __init__(self, binding: DataSourceBinding) -> None:
        self._binding = binding

    def for_project(self, project_id: str) -> DataSourceBinding:  # noqa: ARG002
        return self._binding

    def invalidate(self, data_source_id: str) -> None:  # noqa: ARG002
        return None

    def _unavailable(self):
        return DataSourceError(
            "data source management is not enabled on this deployment",
            code="DATA_SOURCE_MANAGEMENT_DISABLED",
        )

    def list(self) -> tuple[DataSourceRecord, ...]:
        return ()

    def get(self, data_source_id: str) -> DataSourceRecord | None:  # noqa: ARG002
        return None

    def project_data_source_id(self, project_id: str) -> str | None:  # noqa: ARG002
        return None

    def create(self, **_: object):
        raise self._unavailable()

    def update(self, **_: object):
        raise self._unavailable()

    def delete(self, *_: object, **__: object):
        raise self._unavailable()

    def bind(self, **_: object):
        raise self._unavailable()

    def test(self, **_: object):
        raise self._unavailable()

    def close(self) -> None:
        if self._binding.engine is not None:
            self._binding.engine.dispose()
