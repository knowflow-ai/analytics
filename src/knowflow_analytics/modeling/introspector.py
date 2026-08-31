from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import Engine, bindparam, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.modeling.contracts import (
    ForeignKeySnapshot,
    SchemaColumnSnapshot,
    SchemaSnapshot,
    TableCatalogEntry,
    TableSnapshot,
)


class SchemaIntrospectionError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "SCHEMA_INTROSPECTION_FAILED") -> None:
        super().__init__(message, code=code, stage="MODELING_INTROSPECTION")


class PostgreSqlIntrospector:
    """Read a bounded PostgreSQL schema snapshot without exposing credentials."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def connection_test(self) -> dict[str, str]:
        try:
            with self._engine.connect() as connection, connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                row = connection.execute(
                    text("SELECT current_database(), current_setting('server_version')")
                ).one()
            return {"database": str(row[0]), "server_version": str(row[1])}
        except SQLAlchemyError as exc:
            raise SchemaIntrospectionError("PostgreSQL connection validation failed") from exc

    def list_schemas(self) -> tuple[str, ...]:
        """List user schemas without creating a modeling revision."""

        try:
            names = inspect(self._engine).get_schema_names()
        except SQLAlchemyError as exc:
            raise SchemaIntrospectionError("PostgreSQL schema listing failed") from exc
        return tuple(
            sorted(
                name
                for name in set(str(item) for item in names)
                if name != "information_schema" and not name.startswith("pg_")
            )
        )

    def list_tables(
        self,
        *,
        schema_name: str,
        include_views: bool = False,
    ) -> tuple[TableCatalogEntry, ...]:
        """List table choices for the UI without importing any model."""

        inspector = inspect(self._engine)
        try:
            table_names = set(inspector.get_table_names(schema=schema_name))
            view_names = (
                set(inspector.get_view_names(schema=schema_name)) if include_views else set()
            )
            entries: list[TableCatalogEntry] = []
            for source_type, names in (("table", table_names), ("view", view_names)):
                for name in sorted(names):
                    try:
                        comment = str(
                            inspector.get_table_comment(name, schema=schema_name).get("text") or ""
                        )
                    except NotImplementedError:
                        comment = ""
                    entries.append(
                        TableCatalogEntry(
                            schema_name=schema_name,
                            name=name,
                            source_type=source_type,
                            comment=comment,
                        )
                    )
            return tuple(entries)
        except SQLAlchemyError as exc:
            raise SchemaIntrospectionError("PostgreSQL table listing failed") from exc

    def describe_table(
        self,
        *,
        schema_name: str,
        table_name: str,
        include_views: bool = False,
    ) -> TableSnapshot:
        snapshot = self.scan(
            schemas=(schema_name,),
            selected_tables={schema_name: (table_name,)},
            include_views=include_views,
        )
        return snapshot.tables[0]

    def describe_query(self, sql_query: str) -> tuple[SchemaColumnSnapshot, ...]:
        """Return output columns for one already-governed read-only SQL model."""

        try:
            with self._engine.connect() as connection, connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
                result = connection.execute(
                    text(f"SELECT * FROM ({sql_query}) AS governed_sql_model LIMIT 0")
                )
                names = tuple(map(str, result.keys()))
                description = tuple(result.cursor.description if result.cursor else ())
                type_oids = {
                    int(item.type_code)
                    for item in description
                    if getattr(item, "type_code", None) is not None
                }
                type_names: dict[int, str] = {}
                if type_oids:
                    rows = connection.execute(
                        text(
                            "SELECT oid::integer, format_type(oid, NULL) "
                            "FROM pg_type WHERE oid IN :oids"
                        ).bindparams(bindparam("oids", expanding=True)),
                        {"oids": sorted(type_oids)},
                    )
                    type_names = {int(row[0]): str(row[1]) for row in rows}
            if len(names) != len(set(item.casefold() for item in names)):
                raise SchemaIntrospectionError(
                    "SQL model output columns must be unique",
                    code="SQL_MODEL_DUPLICATE_COLUMNS",
                )
            return tuple(
                SchemaColumnSnapshot(
                    name=name,
                    data_type=type_names.get(
                        int(description[index].type_code),
                        "text",
                    ),
                    nullable=True,
                    ordinal_position=index,
                )
                for index, name in enumerate(names)
            )
        except SchemaIntrospectionError:
            raise
        except SQLAlchemyError as exc:
            raise SchemaIntrospectionError(
                "SQL model validation query failed",
                code="SQL_MODEL_QUERY_FAILED",
            ) from exc

    def scan(
        self,
        *,
        schemas: Sequence[str],
        selected_tables: Mapping[str, Sequence[str]] | None = None,
        include_views: bool = False,
    ) -> SchemaSnapshot:
        if not schemas:
            raise SchemaIntrospectionError("at least one schema is required", code="EMPTY_SCOPE")
        normalized_schemas = tuple(dict.fromkeys(str(item or "").strip() for item in schemas))
        if any(not item for item in normalized_schemas):
            raise SchemaIntrospectionError("schema names are invalid", code="INVALID_SCOPE")
        if selected_tables is not None:
            unknown_schemas = set(selected_tables).difference(normalized_schemas)
            if unknown_schemas:
                raise SchemaIntrospectionError(
                    f"selected tables contain unknown schemas: {sorted(unknown_schemas)}",
                    code="UNKNOWN_SCHEMA_SCOPE",
                )
        inspector = inspect(self._engine)
        tables: list[TableSnapshot] = []
        try:
            database_name = self.connection_test()["database"]
            for schema_name in sorted(normalized_schemas):
                available_tables = set(inspector.get_table_names(schema=schema_name))
                available_views = (
                    set(inspector.get_view_names(schema=schema_name)) if include_views else set()
                )
                requested = (
                    set(selected_tables.get(schema_name, ()))
                    if selected_tables is not None
                    else None
                )
                if requested is not None:
                    missing = requested - available_tables - available_views
                    if missing:
                        raise SchemaIntrospectionError(
                            f"tables are outside the selected schema snapshot: {sorted(missing)}",
                            code="UNKNOWN_TABLE",
                        )
                    available_tables &= requested
                    available_views &= requested
                for table_name in sorted(available_tables):
                    tables.append(self._read_table(inspector, schema_name, table_name, "table"))
                for view_name in sorted(available_views):
                    tables.append(self._read_table(inspector, schema_name, view_name, "view"))
        except SchemaIntrospectionError:
            raise
        except SQLAlchemyError as exc:
            raise SchemaIntrospectionError("PostgreSQL schema scan failed") from exc
        if not tables:
            raise SchemaIntrospectionError("selected scope contains no tables", code="EMPTY_SCOPE")
        return SchemaSnapshot.create(
            database_name=database_name,
            tables=tuple(tables),
            captured_at=datetime.now(UTC),
        )

    @staticmethod
    def _read_table(
        inspector: object,
        schema_name: str,
        table_name: str,
        source_type: str,
    ) -> TableSnapshot:
        columns_raw = inspector.get_columns(table_name, schema=schema_name)  # type: ignore[attr-defined]
        pk = inspector.get_pk_constraint(table_name, schema=schema_name)  # type: ignore[attr-defined]
        primary_columns = set(pk.get("constrained_columns") or ())
        unique_columns: set[str] = set()
        for constraint in inspector.get_unique_constraints(  # type: ignore[attr-defined]
            table_name, schema=schema_name
        ):
            names = constraint.get("column_names") or ()
            if len(names) == 1:
                unique_columns.add(str(names[0]))
        columns = tuple(
            SchemaColumnSnapshot(
                name=str(column["name"]),
                data_type=str(column["type"]),
                nullable=bool(column.get("nullable", True)),
                comment=str(column.get("comment") or ""),
                ordinal_position=index,
                primary_key=str(column["name"]) in primary_columns,
                unique=str(column["name"]) in unique_columns,
            )
            for index, column in enumerate(columns_raw)
        )
        foreign_keys = tuple(
            ForeignKeySnapshot(
                name=foreign_key.get("name"),
                constrained_columns=tuple(foreign_key.get("constrained_columns") or ()),
                referred_schema=str(foreign_key.get("referred_schema") or schema_name),
                referred_table=str(foreign_key["referred_table"]),
                referred_columns=tuple(foreign_key.get("referred_columns") or ()),
            )
            for foreign_key in inspector.get_foreign_keys(  # type: ignore[attr-defined]
                table_name, schema=schema_name
            )
        )
        try:
            comment = str(
                inspector.get_table_comment(table_name, schema=schema_name).get("text") or ""  # type: ignore[attr-defined]
            )
        except NotImplementedError:
            comment = ""
        return TableSnapshot(
            schema_name=schema_name,
            name=table_name,
            source_type=source_type,
            comment=comment,
            columns=columns,
            foreign_keys=foreign_keys,
        )
