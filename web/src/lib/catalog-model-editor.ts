import type {
  AnalyticsCatalogModel,
  AnalyticsCatalogModelDimension,
  AnalyticsFieldKind,
} from '@analytics/api/types';

export interface CatalogModelBasicInput {
  name: string;
  bizName: string;
  description: string;
  alias: string | null;
  filterSql: string | null;
}

export interface CatalogFieldRoleInput {
  name: string;
  kind: AnalyticsFieldKind;
  identifierType?: 'primary' | 'foreign';
  dimensionType?: 'categorical' | 'time' | 'partition_time';
  aggregation?: 'sum' | 'count' | 'count_distinct' | 'avg' | 'min' | 'max';
  unit?: string;
  createDimension?: boolean;
  createMetric?: boolean;
}

export function updateCatalogModelBasic(
  model: AnalyticsCatalogModel,
  input: CatalogModelBasicInput,
): AnalyticsCatalogModel {
  return {
    ...model,
    name: input.name,
    bizName: input.bizName,
    description: input.description,
    alias: input.alias,
    filterSql: input.filterSql,
    modelDetail: {
      ...model.modelDetail,
      filterSql: input.filterSql,
    },
  };
}

export function updateCatalogModelFieldRole(
  model: AnalyticsCatalogModel,
  fieldName: string,
  input: CatalogFieldRoleInput,
): AnalyticsCatalogModel {
  const physical = model.modelDetail.fields.find(
    (field) => field.fieldName === fieldName,
  );
  if (!physical) throw new Error(`Catalog model field not found: ${fieldName}`);

  const previousIdentifier = model.modelDetail.identifiers.find(
    (item) => item.bizName === fieldName,
  );
  const previousDimension = model.modelDetail.dimensions.find(
    (item) => item.expr === fieldName,
  );
  const previousMeasure = model.modelDetail.measures.find(
    (item) => item.expr === fieldName,
  );
  const identifiers = model.modelDetail.identifiers.filter(
    (item) => item.bizName !== fieldName,
  );
  const dimensions = model.modelDetail.dimensions.filter(
    (item) => item.expr !== fieldName,
  );
  const measures = model.modelDetail.measures.filter(
    (item) => item.expr !== fieldName,
  );

  if (input.kind === 'identifier') {
    identifiers.push({
      name: input.name,
      type: input.identifierType ?? previousIdentifier?.type ?? 'primary',
      bizName: fieldName,
      isCreateDimension: input.createDimension ? 1 : 0,
    });
  } else if (input.kind === 'dimension' || input.kind === 'time') {
    const dimensionType =
      input.kind === 'time'
        ? input.dimensionType ??
          (previousDimension?.type === 'time' ||
          previousDimension?.type === 'partition_time'
            ? previousDimension.type
            : 'time')
        : input.dimensionType ?? 'categorical';
    const dimension: AnalyticsCatalogModelDimension = {
      name: input.name,
      type: dimensionType,
      expr: fieldName,
      dateFormat: previousDimension?.dateFormat ?? 'yyyy-MM-dd',
      dataType: physical.dataType,
      typeParams: previousDimension?.typeParams ?? null,
      isCreateDimension: input.createDimension ? 1 : 0,
      bizName: previousDimension?.bizName ?? fieldName,
      description: previousDimension?.description ?? '',
    };
    dimensions.push(dimension);
  } else if (input.kind === 'measure') {
    measures.push({
      name: input.name,
      agg: (input.aggregation ?? previousMeasure?.agg ?? 'sum').toUpperCase(),
      expr: fieldName,
      bizName: previousMeasure?.bizName ?? fieldName,
      isCreateMetric: input.createMetric ? 1 : 0,
      constraint: previousMeasure?.constraint ?? null,
      alias: previousMeasure?.alias ?? null,
      unit: input.unit ?? previousMeasure?.unit ?? null,
    });
  }

  // 普通字段没有角色对象可以带名字，业务名落在物理字段条目上；与列名相同
  // 视为没起名。指定了角色的字段名字由角色对象承载，物理条目不动。
  const fields =
    input.kind === 'field'
      ? model.modelDetail.fields.map((field) => {
          if (field.fieldName !== fieldName) return field;
          const name = input.name.trim();
          return { ...field, name: name && name !== fieldName ? name : null };
        })
      : model.modelDetail.fields;

  return {
    ...model,
    modelDetail: {
      ...model.modelDetail,
      fields,
      identifiers,
      dimensions,
      measures,
    },
  };
}
