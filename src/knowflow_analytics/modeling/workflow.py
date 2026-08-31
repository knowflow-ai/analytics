"""固定工作流的 S4（命名）与 S6（汇合）：把 S2 角色、S3 预填、S1 画像组装成
与单次大调用完全相同的 ``ModelSchemaContract``，下游一行不改。

32B 面对"给 20 列起中文名"比"150 个决策全对"稳得多：分类和聚合由 S3 / S5 决定，
模型在这里只负责 name / description / unit。返回的列名不在本块清单里就丢
（WrenAI 的反幻觉过滤）；缺的列用注释或列名兜底并计数。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from pydantic import ValidationError

from knowflow_analytics.contracts import Aggregation, FieldKind, FieldSpec
from knowflow_analytics.gateways.model import ModelGatewayError, StructuredModelGateway
from knowflow_analytics.modeling.catalog_contracts import (
    AggOperator,
    ModelSchemaContract,
    SemanticColumnContract,
    SemanticColumnType,
    SemanticMetricContract,
)
from knowflow_analytics.modeling.classify import Prefill
from knowflow_analytics.modeling.contracts import TableSnapshot
from knowflow_analytics.modeling.profile import ColumnProfile, TableProfile
from knowflow_analytics.modeling.prompts import (
    CLASSIFY_SYSTEM,
    NAMING_SYSTEM,
    ClassifyOutput,
    NamedColumn,
    NamingOutput,
    TableRoleOutput,
)
from knowflow_analytics.modeling.topology import TableTopology

LOGGER = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 20
_NAMING_ATTEMPTS = 2


@dataclass
class NamingConventions:
    """本轮已定案的跨表可复用名字。

    只收录含义有锚点的列——非主键标识列（外键/待确认标识），它们的含义锚在
    所指实体上，`订单.客户ID` 与 `投诉.客户ID` 就该同名。实体自身属性列
    （名称/地址/类型……）绝不入册：城市表先把 名称 定为「城市名称」后，
    图书馆表的 名称 若直接沿用，整个目录就不存在「图书馆名称」，且「城市」
    一词被劫持（2026-08-28 城市/图书馆回归 3/12 的直接根因）。这类列逐表
    询问模型；兜底永远是零信息的裸列名，绝不是借来的实体名。"""

    names: dict[str, str] = field(default_factory=dict)
    table_names: dict[tuple[str, str], str] = field(default_factory=dict)

    def remember(self, column: str, name: str) -> None:
        self.names.setdefault(column.casefold(), name)

    def lookup(self, column: str) -> str | None:
        return self.names.get(column.casefold())


@dataclass(frozen=True)
class TableBuildResult:
    contract: ModelSchemaContract
    reasons: dict[str, str]  # column → 分类理由（给 SuggestionPatch.reason）
    prefills: dict[str, Prefill]  # S5 之后的最终预填；_to_patches 的护栏要看这份


class StagedTableModeler:
    def __init__(
        self,
        gateway: StructuredModelGateway,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_concurrency: int = 5,
    ) -> None:
        self._gateway = gateway
        self._chunk_size = max(1, chunk_size)
        self._max_concurrency = max(1, max_concurrency)

    # ------------------------------------------------------------------ S4
    def build_table(
        self,
        *,
        table: TableSnapshot,
        fields: tuple[FieldSpec, ...],
        role: TableRoleOutput | None,
        role_name: str,
        topology: TableTopology | None,
        profile: TableProfile | None,
        prefills: dict[str, Prefill],
        conventions: NamingConventions,
        trace: dict[str, str],
        evidence: tuple = (),
    ) -> TableBuildResult:
        columns = [f for f in fields if f.column in prefills]
        chunks = [
            columns[i : i + self._chunk_size] for i in range(0, len(columns), self._chunk_size)
        ]
        header = self._table_header(table, role, role_name, topology, conventions)
        if evidence:
            # 知识库摘录：不可信的业务资料，只能用于名称、说明和单位（与单次调用时同一标注）。
            header["knowledge_evidence"] = [item.model_dump(mode="json") for item in evidence]

        def name_chunk(index_chunk):
            index, chunk = index_chunk
            return self._name_chunk(
                header=header,
                chunk=chunk,
                prefills=prefills,
                profile=profile,
                conventions=conventions,
                trace={**trace, "chunk": str(index)},
            )

        if len(chunks) <= 1 or self._max_concurrency <= 1:
            named = [name_chunk(item) for item in enumerate(chunks)]
        else:
            with ThreadPoolExecutor(
                max_workers=min(self._max_concurrency, len(chunks)),
                thread_name_prefix="analytics-naming",
            ) as pool:
                named = list(pool.map(name_chunk, enumerate(chunks)))

        by_column: dict[str, NamedColumn] = {}
        fallback_columns: set[str] = set()
        for chunk_names, chunk_fallback in named:
            by_column.update(chunk_names)
            fallback_columns.update(chunk_fallback)

        # S5：判断域列（结论建立在列名启发式或弱先验上）全部盲判，再与规则对账。
        # 以前只送 confidence < 0.8 的列，事实表数值 SUM(0.8) 正好免审——利率求和
        # 就是这么漏的。数据库事实（主外键/时间类型）不进模型。
        reviewed = self._classify_blind(
            header=header,
            columns=[f for f in columns if prefills[f.column].judgment_zone],
            prefills=prefills,
            profile=profile,
            names=by_column,
            trace=trace,
        )
        prefills = {**prefills, **reviewed}

        # 跨表一致仅限锚定列：非主键标识列进约定，后面的表直接沿用。
        # 三类不进约定：模型漏掉、用列名兜底的（那不是名字）；主键（每张表的
        # id 都是自己的「客户ID」「订单ID」，不能共享）；一切非标识列（名称/
        # 地址/类型……描述的是本表实体，借名即劫持）。
        for column, item in by_column.items():
            if item.name == column or item.name == _column_comment(columns, column):
                continue
            if prefills[column].kind is not FieldKind.IDENTIFIER:
                continue
            if prefills[column].identifier_type == "primary":
                continue
            conventions.remember(column, item.name)

        return self._assemble(
            table=table,
            fields=columns,
            role=role,
            prefills=prefills,
            names=by_column,
            fallback_columns=fallback_columns,
            conventions=conventions,
        )

    def _name_chunk(
        self,
        *,
        header: dict[str, object],
        chunk: list[FieldSpec],
        prefills: dict[str, Prefill],
        profile: TableProfile | None,
        conventions: NamingConventions,
        trace: dict[str, str],
    ) -> dict[str, NamedColumn]:
        # 约定里已有的不再问；只把剩下的送给模型。
        settled: dict[str, NamedColumn] = {}
        to_ask: list[FieldSpec] = []
        for f in chunk:
            known = (
                conventions.lookup(f.column)
                if prefills[f.column].kind is FieldKind.IDENTIFIER
                and prefills[f.column].identifier_type != "primary"
                else None
            )
            if known is not None:
                settled[f.column] = NamedColumn(column_name=f.column, name=known)
            else:
                to_ask.append(f)
        if not to_ask:
            return settled, set()

        payload = {
            **header,
            "naming_conventions": {
                f.column: conventions.lookup(f.column)
                for f in chunk
                if conventions.lookup(f.column) is not None
            },
            "columns": [
                {
                    **self._column_payload(f, profile.column(f.column) if profile else None),
                    # 命名需要角色（只有度量配单位）；这是任务输入，不是待复核结论。
                    "kind": prefills[f.column].kind.value,
                }
                for f in to_ask
            ],
        }
        messages = [
            {"role": "system", "content": NAMING_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]
        wanted = {f.column for f in to_ask}
        output: NamingOutput | None = None
        for attempt in range(1, _NAMING_ATTEMPTS + 1):
            try:
                raw = self._gateway.generate_json(
                    purpose="analytics.modeling.naming",
                    messages=messages,
                    response_schema=NamingOutput.model_json_schema(),
                    trace={
                        **trace,
                        "attempt": str(attempt),
                        "contract_version": "knowflow-naming-v1",
                        # 每列名 + 说明 + 单位约 120 token，再留表头余量
                        "max_tokens_hint": str(len(to_ask) * 120 + 400),
                    },
                )
                output = NamingOutput.model_validate(raw)
                break
            except ValidationError as exc:
                # 把校验错误回喂：32B 改错比凭空做对稳得多。
                messages = [
                    *messages,
                    {"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)[:2_000]},
                    {
                        "role": "user",
                        "content": (
                            f"上次输出不符合 JSON Schema：{str(exc)[:500]}。"
                            "请只返回符合 Schema 的对象。"
                        ),
                    },
                ]
            except ModelGatewayError as exc:
                # 一次网关失败就放弃整表命名,兜底成物理列名。不出声的代价见
                # 2026-08-24:三个调用瞬时失败,英文表的目录业务名全是列名,
                # 中文问法 0 命中,而任务显示"完成"。日志 + 决策卡标记双保险。
                LOGGER.warning(
                    "modeling naming call failed for %s; falling back to column names: %s",
                    header.get("table"),
                    exc,
                )
                break
        result = dict(settled)
        if output is not None:
            for item in output.columns:
                if item.column_name in wanted:  # 反幻觉：不在本块清单里的丢
                    result[item.column_name] = item
        # 模型漏掉的列：用注释或列名兜底，名字不会空着。兜底集合必须随结果
        # 一起返回——此前用 setdefault 填回同一个字典,下游按"缺席"检测兜底,
        # 结果永远检测不到(fallback_named 恒为空),2026-08-24 的英文目录就这么
        # 无声通过的。
        fallback: set[str] = set()
        for f in to_ask:
            if f.column not in result:
                fallback.add(f.column)
                result[f.column] = NamedColumn(column_name=f.column, name=f.description or f.column)
        return result, fallback

    def _classify_blind(
        self,
        *,
        header: dict[str, object],
        columns: list[FieldSpec],
        prefills: dict[str, Prefill],
        profile: TableProfile | None,
        names: dict[str, NamedColumn],
        trace: dict[str, str],
    ) -> dict[str, Prefill]:
        """S5 盲判 + S6 对账。

        payload 不含规则结论——独立判定才有校验价值。对账规则：
        一致 → 确认；不一致 → 默认采盲判结论、标 disputed 升级人工；
        模型失败/返回不合法 → 保留规则预填并标 review_skipped（可见，不冒充复核）。
        """

        if not columns:
            return {}

        def skipped() -> dict[str, Prefill]:
            return {
                f.column: prefills[f.column].model_copy(update={"review_skipped": True})
                for f in columns
            }

        payload = {
            "table": header["table"],
            "columns": [
                {
                    "index": i,
                    **self._column_payload(f, profile.column(f.column) if profile else None),
                    "name": names[f.column].name if f.column in names else f.column,
                }
                for i, f in enumerate(columns)
            ],
        }
        try:
            raw = self._gateway.generate_json(
                purpose="analytics.modeling.classify",
                messages=[
                    {"role": "system", "content": CLASSIFY_SYSTEM},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    },
                ],
                response_schema=ClassifyOutput.model_json_schema(),
                trace={**trace, "contract_version": "knowflow-classify-v2"},
            )
            output = ClassifyOutput.model_validate(raw)
        except (ModelGatewayError, ValidationError) as exc:
            LOGGER.warning(
                "modeling classify call failed for %s; keeping rule prefills: %s",
                header.get("table"),
                exc,
            )
            return skipped()
        # 对账主键是 index：模型复述列名时会丢「（万）」后缀、改写全角标点，
        # 名字往返不可靠。精确名与 NFKC 归一名只兜底；归一撞名的键丢弃，宁缺勿错配。
        by_index = {
            item.index: item
            for item in output.columns
            if item.index is not None and 0 <= item.index < len(columns)
        }
        verdicts = {item.column_name: item for item in output.columns}
        normalized: dict[str, object] = {}
        for item in output.columns:
            key = _normalize_column(item.column_name)
            normalized[key] = None if key in normalized else item
        out: dict[str, Prefill] = {}
        for i, f in enumerate(columns):
            rule = prefills[f.column]
            item = (
                by_index.get(i)
                or verdicts.get(f.column)
                or normalized.get(_normalize_column(f.column))
            )
            if item is None:
                out[f.column] = rule.model_copy(update={"review_skipped": True})
                continue
            kind = FieldKind(item.kind)
            if kind is FieldKind.MEASURE and item.aggregation is None:
                # 合同要求 measure 必带聚合；缺了当无效，保留规则预填并明示未复核。
                out[f.column] = rule.model_copy(update={"review_skipped": True})
                continue
            agg = Aggregation(item.aggregation.lower()) if kind is FieldKind.MEASURE else None
            agrees = kind is rule.kind and (
                kind is not FieldKind.MEASURE or agg is rule.aggregation
            )
            if agrees:
                out[f.column] = rule.model_copy(
                    update={
                        "confidence": 0.9,
                        "reason": f"规则与模型独立判定一致：{rule.reason}",
                    }
                )
                continue
            out[f.column] = Prefill(
                column=f.column,
                kind=kind,
                identifier_type="primary" if kind is FieldKind.IDENTIFIER else None,
                dimension_type=(
                    "time"
                    if kind is FieldKind.TIME
                    else "categorical"
                    if kind is FieldKind.DIMENSION
                    else None
                ),
                aggregation=agg,
                confidence=0.5,
                judgment_zone=True,
                disputed=True,
                reason=_dispute_note(rule, kind, agg, item.reason),
            )
        return out

    @staticmethod
    def _table_header(
        table: TableSnapshot,
        role: TableRoleOutput | None,
        role_name: str,
        topology: TableTopology | None,
        conventions: NamingConventions,
    ) -> dict[str, object]:
        related = []
        for item in topology.related if topology else ():
            known = conventions.table_names.get((item.schema_name, item.table))
            related.append(
                {
                    "table": f"{item.schema_name}.{item.table}",
                    **({"name": known} if known else {}),
                    "via": ", ".join(local for local, _ in item.join_columns),
                }
            )
        return {
            "table": {
                "name": f"{table.schema_name}.{table.name}",
                "comment": table.comment,
                "role": role_name,
                **({"grain": role.grain, "description": role.description} if role else {}),
            },
            "related": related,
        }

    @staticmethod
    def _column_payload(f: FieldSpec, profile: ColumnProfile | None) -> dict[str, object]:
        item: dict[str, object] = {
            "column": f.column,
            "type": f.data_type,
            "comment": f.description,
        }
        if profile is not None:
            p: dict[str, object] = {"null_rate": round(profile.null_rate, 3)}
            if profile.distinct_count:
                p["distinct"] = profile.distinct_count
            if profile.min_value is not None:
                p["min"] = profile.min_value
            if profile.max_value is not None:
                p["max"] = profile.max_value
            if profile.sample_values:
                p["values"] = list(profile.sample_values)
            item["profile"] = p
        return item

    # ------------------------------------------------------------------ S6
    @staticmethod
    def _assemble(
        *,
        table: TableSnapshot,
        fields: list[FieldSpec],
        role: TableRoleOutput | None,
        prefills: dict[str, Prefill],
        names: dict[str, NamedColumn],
        fallback_columns: set[str],
        conventions: NamingConventions,
    ) -> TableBuildResult:
        columns: list[SemanticColumnContract] = []
        reasons: dict[str, str] = {}
        metrics: list[SemanticMetricContract] = []
        for f in fields:
            prefill = prefills[f.column]
            named = names.get(f.column)
            if named is None:
                # 正常流程走不到:S4 已为每列兜底。留作防御,并同样计入兜底集合。
                named = NamedColumn(column_name=f.column, name=f.description or f.column)
                fallback_columns = {*fallback_columns, f.column}
            columns.append(
                SemanticColumnContract(
                    column_name=f.column,
                    data_type=f.data_type,
                    comment=named.description,
                    filed_type=_contract_type(prefill),
                    name=named.name,
                    expr=f.column,
                )
            )
            if prefill.kind is FieldKind.MEASURE:
                metrics.append(
                    SemanticMetricContract(
                        column_name=f.column,
                        agg=AggOperator((prefill.aggregation or Aggregation.SUM).value.upper()),
                        unit=named.unit,
                    )
                )
            if prefill.disputed:
                reasons[f.column] = prefill.reason  # 分歧说明本身就是决策卡文案
            elif prefill.review_skipped:
                reasons[f.column] = f"{prefill.reason}（仅规则判定，模型复核未执行）"
            elif prefill.needs_review:
                reasons[f.column] = f"{prefill.reason}（规则存疑，请核对）"
            else:
                reasons[f.column] = prefill.reason
            if f.column in fallback_columns:
                # 兜底名不是名字。不标出来,英文列的目录会顶着物理列名无声通过,
                # 别名再跟着同语言长歪(2026-08-24)。
                reasons[f.column] += "（业务名未生成，暂用注释或列名，请人工命名）"
        table_name = (role.name if role and role.name else None) or table.comment or table.name
        conventions.table_names[(table.schema_name, table.name)] = table_name
        contract = ModelSchemaContract(
            name=table_name,
            biz_name=_biz_name(table.name),
            description=(role.description if role else "") or table.comment,
            semantic_columns=tuple(columns),
            metrics=tuple(metrics),
        )
        return TableBuildResult(
            contract=contract,
            reasons=reasons,
            prefills=dict(prefills),
        )


def _normalize_column(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().casefold()


def _dispute_note(
    rule: Prefill,
    kind: FieldKind,
    agg: Aggregation | None,
    model_reason: str,
) -> str:
    def side(k: FieldKind, a: Aggregation | None, why: str) -> str:
        label = k.value + (f"·{a.value.upper()}" if a else "")
        why = (why or "").strip()
        return f"{label}（{why[:80]}）" if why else label

    return (
        "分歧待裁决 —— 规则："
        + side(rule.kind, rule.aggregation, rule.reason)
        + "；模型："
        + side(kind, agg, model_reason)
    )


def _column_comment(columns: list[FieldSpec], column: str) -> str:
    return next((f.description for f in columns if f.column == column), "")


def _contract_type(prefill: Prefill) -> SemanticColumnType:
    """列在分组与连接里的角色。聚合方式改由独立的 metrics 区块承载。"""

    if prefill.kind is FieldKind.IDENTIFIER:
        return (
            SemanticColumnType.FOREIGN_KEY
            if prefill.identifier_type == "foreign"
            else SemanticColumnType.PRIMARY_KEY
        )
    if prefill.kind is FieldKind.TIME:
        return (
            SemanticColumnType.PARTITION_TIME
            if prefill.dimension_type == "partition_time"
            else SemanticColumnType.TIME
        )
    # 度量、dimension 与未分类（field）在列分类上都是分类维度；度量的实际角色
    # 由 metrics 区块决定，转换时优先于此处的取值。
    return SemanticColumnType.CATEGORICAL


def _biz_name(table_name: str) -> str:
    """从表名确定性派生，不再让模型生成后去匹配正则。"""

    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", table_name).strip("_") or "model"
    return cleaned if re.match(r"^[A-Za-z_]", cleaned) else f"t_{cleaned}"
