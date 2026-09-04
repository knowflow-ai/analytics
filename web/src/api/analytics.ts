import { EDITION, request } from './client';
import type {
  AnalyticsCatalogDimension,
  AnalyticsCatalogHierarchy,
  AnalyticsDictionaryDecision,
  AnalyticsDimensionValue,
  AnalyticsEvaluationReport,
  AnalyticsGoldenSuiteRecord,
  AnalyticsFeedbackStatus,
  AnalyticsQueryFailure,
  AnalyticsQueryDiagnosticExport,
  AnalyticsDictionaryPreview,
  AnalyticsCatalogMetric,
  AnalyticsCatalogModel,
  AnalyticsCatalogRelation,
  AnalyticsCatalogResourceKind,
  AnalyticsModelGraphLayout,
  AnalyticsModelingDiagnostics,
  AnalyticsModelingJob,
  AnalyticsModelingProposal,
  AnalyticsModelingSummary,
  AnalyticsProject,
  AnalyticsPublishedRelease,
  AnalyticsModelingQualityReport,
  AnalyticsQueryResponse,
  AnalyticsReleaseSummary,
  AnalyticsRevision,
  AnalyticsSchemaCatalog,
  AnalyticsSemanticAliasReview,
  AnalyticsSemanticQuery,
  AnalyticsSuggestionDecision,
  AnalyticsTableCatalog,
  AnalyticsTerm,
} from './types';

const PROJECT_ID_PREFIX = 'prj_oss_';
const base = (projectId: string) => `/v1/analytics/projects/${projectId}`;
const revisionPath = (projectId: string, revisionId: string) =>
  `${base(projectId)}/revisions/${revisionId}`;

function catalogResourcePathSegment(resourceId: string): string {
  if (
    resourceId === '.' ||
    resourceId === '..' ||
    /[\/\\\u0000-\u001f\u007f]/u.test(resourceId)
  ) {
    throw new Error('catalog resource id contains an unsafe path segment');
  }
  return encodeURIComponent(resourceId);
}

export function newResourceId(prefix: string): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${prefix}_${hex}`;
}

/** A revision's optimistic-concurrency pair; every write carries both. */
export interface RevisionVersion {
  expected_etag: number;
  schema_snapshot_hash: string;
}

export const versionOf = (revision: AnalyticsRevision): RevisionVersion => ({
  expected_etag: revision.etag,
  schema_snapshot_hash: revision.schema_snapshot_hash,
});

// --- projects -----------------------------------------------------------------

/**
 * 删掉项目及其名下的一切。
 *
 * 走宿主的专办路由（``/core/projects/{id}``），门槛是全局 admin——删项目会毁掉
 * 别人在这个项目上的全部工作，比建项目更不可逆。
 */
export const deleteProject = (projectId: string): Promise<unknown> =>
  request(`/v1/analytics/projects/${encodeURIComponent(projectId)}`, {
    method: 'DELETE',
    projectId,
  });

export const listProjects = () =>
  request<{ items: AnalyticsProject[] }>('/v1/analytics/projects', {
    // 嵌入版不送 id_prefix：宿主的 /core/projects 根本不读这个查询参数，它按当前
    // 登录用户圈范围（归属前缀 + RBAC 授权）。继续送 `prj_oss_` 只会让读代码的人
    // 以为在按开源前缀过滤，而那个前缀在嵌入版里本来就是错的。
    query:
      EDITION === 'embedded'
        ? { limit: 200 }
        : { id_prefix: PROJECT_ID_PREFIX, limit: 200 },
  });

export const createProject = (name: string) => {
  if (EDITION === 'embedded') {
    // 商业版:项目 ID 必须由服务端铸(带 HMAC 归属前缀),客户端送 ID 会被拒。
    return request<AnalyticsProject>('/v1/analytics/projects', {
      method: 'POST',
      body: { name },
    });
  }
  const projectId = newResourceId('prj_oss');
  return request<AnalyticsProject>('/v1/analytics/projects', {
    method: 'POST',
    projectId,
    body: { name, project_id: projectId },
  });
};

export const getModelingSummary = (projectId: string) =>
  request<AnalyticsModelingSummary>(`${base(projectId)}/modeling-summary`, { projectId });

// --- datasource -----------------------------------------------------------------

export const listSchemas = (projectId: string) =>
  request<AnalyticsSchemaCatalog>(`${base(projectId)}/datasources/default/schemas`, {
    projectId,
  });

export const listTables = (projectId: string, schemaName: string, includeViews = false) =>
  request<AnalyticsTableCatalog>(`${base(projectId)}/datasources/default/tables`, {
    projectId,
    query: { schema_name: schemaName, include_views: includeViews },
  });

// --- revisions ------------------------------------------------------------------

export const getRevision = (projectId: string, revisionId: string) =>
  request<AnalyticsRevision>(revisionPath(projectId, revisionId), { projectId });

interface SchemaSnapshot {
  id: string;
  content_hash: string;
  tables: Array<{ schema_name: string; name: string }>;
}

/**
 * Snapshot the selected tables, open a revision and import every table.
 * The core exposes these as three atomic calls; table imports must be serial
 * because each one advances the revision etag.
 */
export async function createDraft(
  projectId: string,
  input: { schemas: string[]; selected_tables: Record<string, string[]>; include_views: boolean },
): Promise<AnalyticsRevision> {
  const snapshot = await request<SchemaSnapshot>(`${base(projectId)}/schema-snapshots`, {
    method: 'POST',
    projectId,
    body: input,
  });
  let revision = await request<AnalyticsRevision>(`${base(projectId)}/revisions`, {
    method: 'POST',
    projectId,
    body: { schema_snapshot_id: snapshot.id },
  });
  for (const table of snapshot.tables) {
    revision = await request<AnalyticsRevision>(
      `${revisionPath(projectId, revision.id)}/models:from-table`,
      {
        method: 'POST',
        projectId,
        body: {
          expected_etag: revision.etag,
          schema_snapshot_hash: snapshot.content_hash,
          schema_name: table.schema_name,
          table_name: table.name,
        },
      },
    );
  }
  return revision;
}

export const extendTables = (
  projectId: string,
  revisionId: string,
  input: RevisionVersion & { selected_tables: Record<string, string[]>; include_views: boolean },
) =>
  request<AnalyticsRevision>(`${revisionPath(projectId, revisionId)}/tables:extend`, {
    method: 'POST',
    projectId,
    body: input,
  });

export const deriveCandidate = (projectId: string, revisionId: string) =>
  request<AnalyticsRevision>(`${revisionPath(projectId, revisionId)}:derive`, {
    method: 'POST',
    projectId,
  });

// --- layout ---------------------------------------------------------------------

export const getGraphLayout = (projectId: string, revisionId: string) =>
  request<AnalyticsModelGraphLayout>(`${revisionPath(projectId, revisionId)}/model-graph-layout`, {
    projectId,
  });

export const saveGraphLayout = (
  projectId: string,
  revisionId: string,
  input: {
    expected_etag: number;
    positions: AnalyticsModelGraphLayout['positions'];
    viewport: AnalyticsModelGraphLayout['viewport'];
  },
) =>
  request<AnalyticsModelGraphLayout>(`${revisionPath(projectId, revisionId)}/model-graph-layout`, {
    method: 'PUT',
    projectId,
    body: input,
  });

// --- catalog relations ----------------------------------------------------------

export const saveRelation = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  relation: AnalyticsCatalogRelation,
) =>
  request<AnalyticsRevision>(
    `${revisionPath(projectId, revisionId)}/catalog/relations/${catalogResourcePathSegment(relation.id)}`,
    { method: 'PUT', projectId, body: { ...version, relation } },
  );

export const deleteRelation = async (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  relationId: string,
) => {
  const path = `${revisionPath(projectId, revisionId)}/catalog/relations/${catalogResourcePathSegment(relationId)}`;
  const impact = await request<{ impact_hash: string }>(`${path}/deletion-impact`, {
    method: 'POST',
    projectId,
    body: version,
  });
  return request<AnalyticsRevision>(path, {
    method: 'DELETE',
    projectId,
    body: { ...version, expected_impact_hash: impact.impact_hash, confirmation: 'delete' },
  });
};

// --- catalog models / dimensions / metrics ------------------------------------

export const saveModel = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  model: AnalyticsCatalogModel,
) =>
  request<AnalyticsRevision>(`${revisionPath(projectId, revisionId)}/catalog/models/${catalogResourcePathSegment(model.id)}`, {
    method: 'PUT',
    projectId,
    body: { ...version, model },
  });

export const saveDimension = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  dimension: AnalyticsCatalogDimension,
) =>
  request<AnalyticsRevision>(
    `${revisionPath(projectId, revisionId)}/catalog/dimensions/${catalogResourcePathSegment(dimension.id)}`,
    { method: 'PUT', projectId, body: { ...version, dimension } },
  );

export const saveMetric = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  metric: AnalyticsCatalogMetric,
) =>
  request<AnalyticsRevision>(`${revisionPath(projectId, revisionId)}/catalog/metrics/${catalogResourcePathSegment(metric.id)}`, {
    method: 'PUT',
    projectId,
    body: { ...version, metric },
  });

/** Business terms are first-class Catalog resources. */
export const saveTerm = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  term: AnalyticsTerm,
) =>
  request<AnalyticsRevision>(
    `${revisionPath(projectId, revisionId)}/catalog/terms/${catalogResourcePathSegment(term.id)}`,
    { method: 'PUT', projectId, body: { ...version, term } },
  );

/** Existing dimension values keep their sampled identity; only presentation is editable. */
export const saveDimensionValue = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  dimensionValue: AnalyticsDimensionValue,
) =>
  request<AnalyticsRevision>(
    `${revisionPath(projectId, revisionId)}/catalog/dimension-values/${catalogResourcePathSegment(dimensionValue.id)}`,
    {
      method: 'PUT',
      projectId,
      body: { ...version, dimension_value: dimensionValue },
    },
  );

/** 发布前用真实只读数据核对:主标识唯一率、关系实测基数、指标样本、可达性。 */
export const getCurrentEvaluation = (projectId: string, revisionId: string) =>
  request<{ report: AnalyticsEvaluationReport | null }>(
    `${revisionPath(projectId, revisionId)}/evaluations%3Alatest`,
    { projectId },
  );

export const getCurrentQualityReport = (projectId: string, revisionId: string) =>
  request<{ report: AnalyticsModelingQualityReport | null }>(
    `${revisionPath(projectId, revisionId)}/quality-reports%3Alatest`,
    { projectId },
  );

export const reviewQualityReport = (
  projectId: string,
  revisionId: string,
  report: Pick<AnalyticsModelingQualityReport, 'id' | 'etag' | 'content_hash'>,
  decisions: Array<{ preview_id: string; confirm: boolean; note?: string }>,
) =>
  request<AnalyticsModelingQualityReport>(
    // 冒号必须转义:不转义时 `{report_id}` 会把 `xxx:review` 整个吃进去,
    // 匹配到 GET 那条路由,POST 得到 405。
    `${revisionPath(projectId, revisionId)}/quality-reports/${encodeURIComponent(report.id)}%3Areview`,
    {
      method: 'POST',
      projectId,
      // expected_etag 是「报告自己的版本」,不是 revision.etag。传错会让
      // modeling_quality_report_is_stale 恒为真,核对永远提交不上去。
      body: {
        expected_etag: report.etag,
        expected_content_hash: report.content_hash,
        decisions,
      },
    },
  );

export const createQualityReport = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
) =>
  request<AnalyticsModelingQualityReport>(`${revisionPath(projectId, revisionId)}/quality-reports`, {
    method: 'POST',
    projectId,
    body: version,
  });

export const saveHierarchy = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  hierarchy: AnalyticsCatalogHierarchy,
) =>
  request<AnalyticsRevision>(
    `${revisionPath(projectId, revisionId)}/catalog/hierarchies/${catalogResourcePathSegment(hierarchy.id)}`,
    { method: 'PUT', projectId, body: { ...version, hierarchy } },
  );

/** 维度值字典:从真实数据采集取值 → 人工定显示名与别名 → 应用进目录。 */
export const generateDictionaryPreview = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  dimensionIds: string[],
) =>
  request<AnalyticsDictionaryPreview>(
    `${revisionPath(projectId, revisionId)}/dimension-dictionary/previews`,
    { method: 'POST', projectId, body: { ...version, dimension_ids: dimensionIds } },
  );

export const applyDictionaryPreview = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  previewId: string,
  decisions: AnalyticsDictionaryDecision[],
) =>
  request<AnalyticsRevision>(
    `${revisionPath(projectId, revisionId)}/dimension-dictionary/previews/${previewId}/apply`,
    { method: 'POST', projectId, body: { ...version, confirmation: 'apply', decisions } },
  );

/** 为一个指标/维度生成候选别名;只是建议,人工删改后随资源一起保存。 */
export const suggestAliases = (
  projectId: string,
  revisionId: string,
  expectedEtag: number,
  input: {
    resource_type: 'dimension' | 'metric';
    model_id: string;
    name: string;
    biz_name: string;
    description?: string;
    existing_aliases?: string[];
  },
) =>
  request<{ aliases: string[] }>(`${revisionPath(projectId, revisionId)}/alias-suggestions`, {
    method: 'POST',
    projectId,
    body: { expected_etag: expectedEtag, ...input },
  });

export interface QueryFailurePage {
  items: AnalyticsQueryFailure[];
  /** 符合当前筛选的总条数。没有它就说不出「还剩多少条待处理」。 */
  total: number;
  offset: number;
  limit: number;
}

export const listQueryFailures = (
  projectId: string,
  options: {
    limit?: number;
    offset?: number;
    /** `archived` = resolved + ignored，界面上不区分这两者。 */
    status?: AnalyticsFeedbackStatus | 'archived' | 'all';
  } = {},
) =>
  request<QueryFailurePage>(`${base(projectId)}/query-failures`, {
    projectId,
    query: {
      limit: options.limit ?? 50,
      offset: options.offset ?? 0,
      status: options.status ?? 'open',
    },
  });

/** 标成已处理/忽略。没有删除——处理过的收起来，不是抹掉。 */
export const updateQueryFailureStatus = (
  projectId: string,
  input: { failure_ids: number[]; status: AnalyticsFeedbackStatus },
) =>
  request<{ changed: number }>(`${base(projectId)}/query-failures:status`, {
    projectId,
    method: 'POST',
    body: input,
  });

/**
 * 把线上问数切到本项目的某个已发布版本。
 *
 * 取代了原先无参数的 `releases:rollback`——它只往更早走一步，且没有回头路：
 * 回滚一次后线上停在最早那版，更新的版本仍列在历史里却再也切不回去。
 */
export const activateRelease = (projectId: string, releaseId: string) =>
  request<{ active_release_id: string | null }>(
    `${base(projectId)}/releases/${encodeURIComponent(releaseId)}:activate`,
    { method: 'POST', projectId },
  );

// --- 评测集 ---------------------------------------------------------------

export const listGoldenSuites = (projectId: string, revisionId: string) =>
  request<{ items: AnalyticsGoldenSuiteRecord[] }>(
    `${revisionPath(projectId, revisionId)}/golden-suites`,
    { projectId },
  );

export const saveGoldenSuite = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  suite: AnalyticsGoldenSuiteRecord['suite'],
) =>
  request<AnalyticsGoldenSuiteRecord>(
    `${revisionPath(projectId, revisionId)}/golden-suites/${suite.id}`,
    { method: 'PUT', projectId, body: { ...version, suite } },
  );

export const deleteGoldenSuite = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  suiteId: string,
) =>
  request<{ deleted: boolean }>(
    `${revisionPath(projectId, revisionId)}/golden-suites/${suiteId}`,
    { method: 'DELETE', projectId, body: version },
  );

export const evaluateSuite = (
  projectId: string,
  revisionId: string,
  suite: AnalyticsGoldenSuiteRecord['suite'],
) =>
  request<AnalyticsEvaluationReport>(`${revisionPath(projectId, revisionId)}/evaluate`, {
    method: 'POST',
    projectId,
    body: { suite, required_accuracy: 1.0 },
  });

/** 只预览删除影响(服务端规范化的级联清单),不执行删除。 */
export const previewCatalogDeletion = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  kind: AnalyticsCatalogResourceKind,
  resourceId: string,
) =>
  request<{
    impact_hash: string;
    effects: Array<{ action: string; resource_kind: string; resource_id: string; reason?: string }>;
  }>(`${revisionPath(projectId, revisionId)}/catalog/${kind}/${catalogResourcePathSegment(resourceId)}/deletion-impact`, {
    method: 'POST',
    projectId,
    body: version,
  });

/** Two-step governed delete: impact preview, then delete bound to that impact hash. */
export const deleteCatalogResource = async (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  kind: AnalyticsCatalogResourceKind,
  resourceId: string,
  reviewedImpactHash?: string,
) => {
  const path = `${revisionPath(projectId, revisionId)}/catalog/${kind}/${catalogResourcePathSegment(resourceId)}`;
  // A UI that has already shown the impact must submit that exact hash. Falling
  // back to a preview keeps non-interactive callers on the governed two-step API.
  const impactHash = reviewedImpactHash ?? (
    await request<{ impact_hash: string }>(`${path}/deletion-impact`, {
      method: 'POST',
      projectId,
      body: version,
    })
  ).impact_hash;
  return request<AnalyticsRevision>(path, {
    method: 'DELETE',
    projectId,
    body: { ...version, expected_impact_hash: impactHash, confirmation: 'delete' },
  });
};

// --- AI modeling ----------------------------------------------------------------

export const startModelingJob = (projectId: string, revisionId: string, expectedEtag: number) =>
  request<AnalyticsModelingJob>(`${revisionPath(projectId, revisionId)}/modeling-jobs`, {
    method: 'POST',
    projectId,
    body: { expected_etag: expectedEtag },
  });

export const getModelingJob = (projectId: string, jobId: string) =>
  request<AnalyticsModelingJob>(`${base(projectId)}/modeling-jobs/${jobId}`, { projectId });

export const cancelModelingJob = (projectId: string, jobId: string) =>
  request<AnalyticsModelingJob>(`${base(projectId)}/modeling-jobs/${jobId}:cancel`, {
    method: 'POST',
    projectId,
  });

export const getProposal = (projectId: string, revisionId: string, proposalId: string) =>
  request<AnalyticsModelingProposal>(
    `${revisionPath(projectId, revisionId)}/modeling-proposals/${proposalId}`,
    { projectId },
  );

export const saveProposal = (
  projectId: string,
  revisionId: string,
  proposalId: string,
  input: {
    expected_proposal_etag: number;
    expected_proposal_hash: string;
    decisions: AnalyticsSuggestionDecision[];
    alias_reviews: AnalyticsSemanticAliasReview[];
  },
) =>
  request<AnalyticsModelingProposal>(
    `${revisionPath(projectId, revisionId)}/modeling-proposals/${proposalId}`,
    { method: 'PUT', projectId, body: input },
  );

export const applyProposal = (
  projectId: string,
  revisionId: string,
  proposalId: string,
  input: RevisionVersion & { expected_proposal_etag: number; expected_proposal_hash: string },
) =>
  request<{ proposal: AnalyticsModelingProposal; revision: AnalyticsRevision }>(
    `${revisionPath(projectId, revisionId)}/modeling-proposals/${proposalId}:apply`,
    { method: 'POST', projectId, body: { ...input, confirmation: 'apply' } },
  );

// --- validate / publish ---------------------------------------------------------

export const validateRevision = (projectId: string, revisionId: string) =>
  request<AnalyticsRevision>(`${revisionPath(projectId, revisionId)}/validate`, {
    method: 'POST',
    projectId,
  });

export const getDiagnostics = (projectId: string, revisionId: string) =>
  request<AnalyticsModelingDiagnostics>(`${revisionPath(projectId, revisionId)}/diagnostics`, {
    projectId,
  });

export const publishRevision = (projectId: string, revisionId: string, version: RevisionVersion) =>
  request<AnalyticsPublishedRelease>(`${revisionPath(projectId, revisionId)}/publish`, {
    method: 'POST',
    projectId,
    body: { ...version, confirmation: 'publish' },
  });

export const listReleases = (projectId: string) =>
  request<{ items: AnalyticsReleaseSummary[] }>(`${base(projectId)}/releases`, { projectId });

export const getRelease = (projectId: string, releaseId: string) =>
  request<AnalyticsPublishedRelease>(`${base(projectId)}/releases/${releaseId}`, { projectId });

// --- asking ---------------------------------------------------------------------

export interface QueryInput {
  question: string;
  dataset_ids: string[];
  conversation_id?: string;
  selected_candidate_id?: string;
  expected_release_id?: string;
  expected_spec_hash?: string;
  expected_index_snapshot_id?: string;
}

/** Ask the active release (what end users do). */
export const query = (projectId: string, input: QueryInput) =>
  request<AnalyticsQueryResponse>('/v1/analytics/query', {
    method: 'POST',
    projectId,
    body: {
      project_id: projectId,
      ...input,
      include_diagnostics: false,
      include_debug_sql: false,
    },
  });

/** Ask an unpublished candidate revision (what the modeler does to verify). */
export const previewQuery = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  input: QueryInput,
) =>
  request<AnalyticsQueryResponse>(`${revisionPath(projectId, revisionId)}/query-preview`, {
    method: 'POST',
    projectId,
    body: {
      ...version,
      ...input,
      include_diagnostics: true,
      include_debug_sql: true,
    },
  });

/** Structured playground: submit a governed SemanticQuery, skipping NL parsing. */
export const previewStructuredQuery = (
  projectId: string,
  revisionId: string,
  version: RevisionVersion,
  semantic_query: AnalyticsSemanticQuery,
) =>
  request<AnalyticsQueryResponse>(`${revisionPath(projectId, revisionId)}/structured-query-preview`, {
    method: 'POST',
    projectId,
    body: {
      ...version,
      semantic_query,
      include_debug_sql: true,
    },
  });

/** Export the immutable, server-authored evidence for one completed query attempt. */
export const exportQueryDiagnostic = (projectId: string, queryId: string) =>
  request<AnalyticsQueryDiagnosticExport>(
    `${base(projectId)}/query-diagnostics/export`,
    { projectId, query: { query_id: queryId } },
  );

// --- 问数项目授权（仅嵌入版；开源独立版不提供多用户 RBAC）-------------------
// 走宿主路径而非核心：授权是商业版能力，核心不认识授权。client 的 rewritePath
// 只改写 /v1/analytics/ 开头的路径，宿主路径原样发出并自带鉴权头。
//
// 注意信封差异：client 不解包响应，直接返回整个 payload。核心接口没有信封，
// 宿主接口有 `{code, data, message}`——所以这里必须自己剥一层，否则拿到的是
// 信封本身，看起来就是"接口没数据"。

export type GrantSubjectType = 'user' | 'org' | 'group';
export type ProjectRole = 'admin' | 'editor' | 'viewer';

export interface ProjectGrants {
  // 实机返回的用户名字段是 nickname（不是 username）；两个都收，取到哪个用哪个。
  users: Array<{
    user_id: string;
    nickname?: string;
    username?: string;
    role_code: string;
  }>;
  // 实机字段名是 org_name / group_name（不是 name）；两种都收。
  orgs: Array<{
    org_unit_id: string;
    org_name?: string;
    name?: string;
    role_code: string;
  }>;
  groups: Array<{
    group_id: string;
    group_name?: string;
    name?: string;
    role_code: string;
  }>;
}

const HOST_GRANT_BASE = '/v1/kb_folder';

export const listProjectGrants = async (projectId: string): Promise<ProjectGrants> => {
  const data = hostPayload(
    await request<unknown>(
      `${HOST_GRANT_BASE}/analytics_project_grants?project_id=${encodeURIComponent(projectId)}`,
    ),
  ) as Partial<ProjectGrants> | null;
  return {
    users: data?.users ?? [],
    orgs: data?.orgs ?? [],
    groups: data?.groups ?? [],
  };
};

export const grantProject = (
  projectId: string,
  body: { subject_type: GrantSubjectType; subject_id: string; role_code: ProjectRole },
) =>
  request<boolean>(`${HOST_GRANT_BASE}/analytics_project_grant`, {
    method: 'POST',
    body: { project_id: projectId, ...body },
  });

export const revokeProject = (
  projectId: string,
  body: { subject_type: GrantSubjectType; subject_id: string; role_code: ProjectRole },
) =>
  request<boolean>(`${HOST_GRANT_BASE}/analytics_project_revoke`, {
    method: 'POST',
    body: { project_id: projectId, ...body },
  });

export interface GrantSubjectOption {
  id: string;
  name: string;
}

// --- 数据范围（行列级权限）-------------------------------------------------
// 与授权分两层：授权决定能不能进这个项目，数据范围决定进来之后看得到哪些实体、
// 哪些行。两项都空 = 不收窄。

export interface DataScopeRowFilter {
  dimension_id: string;
  operator: 'eq' | 'in';
  value: string;
}

export interface DataScope {
  visible_model_ids: string[];
  row_filters: DataScopeRowFilter[];
}

export interface DataScopeOptions {
  models: Array<{ id: string; name: string }>;
  dimensions: Array<{ id: string; name: string; model_id: string }>;
}

export const fetchDataScopeOptions = async (
  projectId: string,
): Promise<DataScopeOptions> => {
  const data = hostPayload(
    await request<unknown>(
      `${HOST_GRANT_BASE}/analytics_project_data_scope_options?project_id=${encodeURIComponent(projectId)}`,
    ),
  ) as Partial<DataScopeOptions> | null;
  return { models: data?.models ?? [], dimensions: data?.dimensions ?? [] };
};

export const fetchDataScope = async (
  projectId: string,
  subject: { subject_type: GrantSubjectType; subject_id: string },
): Promise<DataScope> => {
  const query = new URLSearchParams({
    project_id: projectId,
    subject_type: subject.subject_type,
    subject_id: subject.subject_id,
  });
  const data = hostPayload(
    await request<unknown>(`${HOST_GRANT_BASE}/analytics_project_data_scope?${query}`),
  ) as Partial<DataScope> | null;
  return {
    visible_model_ids: data?.visible_model_ids ?? [],
    row_filters: data?.row_filters ?? [],
  };
};

export const saveDataScope = (
  projectId: string,
  subject: { subject_type: GrantSubjectType; subject_id: string },
  scope: DataScope,
) =>
  request<boolean>(`${HOST_GRANT_BASE}/analytics_project_data_scope_set`, {
    method: 'POST',
    body: { project_id: projectId, ...subject, ...scope },
  });

/** 宿主接口统一是 `{code, data, message}`；核心接口没有信封。 */
function hostPayload(response: unknown): unknown {
  if (response && typeof response === 'object' && 'code' in response) {
    return (response as { data?: unknown }).data;
  }
  return response;
}

/** 组织是一棵树，按层级缩进拍平（与知识库授权面板同一处理）。 */
function flattenOrgTree(nodes: unknown, depth = 0): GrantSubjectOption[] {
  const out: GrantSubjectOption[] = [];
  for (const node of Array.isArray(nodes) ? nodes : []) {
    const item = node as { id?: unknown; name?: unknown; children?: unknown };
    if (item.id === undefined || item.id === null) continue;
    out.push({
      id: String(item.id),
      name: `${'\u3000'.repeat(depth)}${String(item.name ?? item.id)}`,
    });
    out.push(...flattenOrgTree(item.children, depth + 1));
  }
  return out;
}

/**
 * 授权面板的主体数据源（宿主转发 knowflow），与知识库授权面板复用同一组接口。
 *
 * 三类主体的查询参数与返回结构各不相同，必须逐类处理，不能套一个通用解析：
 * 用户 `username=`、组织 `keyword=`、协作组 `name=`；用户与协作组返回
 * `{list:[...]}`（或直接是数组），组织返回**树**、需要递归拍平。
 */
export async function searchGrantSubjects(
  kind: GrantSubjectType,
  keyword: string,
): Promise<GrantSubjectOption[]> {
  const query = (name: string) =>
    keyword ? `?${name}=${encodeURIComponent(keyword)}` : '';

  if (kind === 'org') {
    const data = hostPayload(
      await request<unknown>(`${HOST_GRANT_BASE}/subjects/orgs${query('keyword')}`),
    );
    return flattenOrgTree(data);
  }

  const path = kind === 'user' ? 'users' : 'groups';
  const data = hostPayload(
    await request<unknown>(
      `${HOST_GRANT_BASE}/subjects/${path}${query(kind === 'user' ? 'username' : 'name')}`,
    ),
  );
  const rows = Array.isArray(data)
    ? data
    : Array.isArray((data as { list?: unknown[] })?.list)
      ? ((data as { list: unknown[] }).list ?? [])
      : [];
  return rows
    .map((row) => {
      const item = row as Record<string, unknown>;
      const id = item.id ?? '';
      const name =
        kind === 'user'
          ? (item.nickname ?? item.username ?? item.email ?? id)
          : (item.name ?? id);
      return { id: String(id), name: String(name) };
    })
    .filter((item) => item.id);
}

// ---- 数据源 ---------------------------------------------------------------
//
// 全局数据源不在任何项目之下，走的是宿主 BFF 的专办路由（核心的直通道只放行
// projects/{id}/... ）。项目绑定那两个端点仍走直通道，天然沿用项目归属判定。
//
// **连接串只进不出。** 请求里带 dsn，返回值里没有装它的地方——这不是"记得别显示"，
// 是类型上就没有那个字段。

// 写成核心的路径，由 client 的 rewritePath 决定去哪：嵌入版转成
// `/v1/analytics/core/...`（宿主 BFF 的专办路由，带全局 admin 闸门），独立版原样
// 打核心。**不要在这里写死 `/core`**——那会被再重写一次，变成 `core/core`，静默 404。
const DATA_SOURCE_BASE = '/v1/analytics';

export interface DataSource {
  id: string;
  name: string;
  /** 'postgres' | 'mysql'。用后端给的原值，前端不做枚举，多一处就多一处会漂的真相。 */
  engine: string;
  created_at?: string;
  updated_at?: string;
}

export const listDataSources = async (): Promise<DataSource[]> => {
  const data = (await request<{ items?: DataSource[] }>(
    `${DATA_SOURCE_BASE}/data-sources`,
  )) as { items?: DataSource[] } | null;
  return data?.items ?? [];
};

export const createDataSource = (input: {
  name: string;
  engine: string;
  dsn: string;
}): Promise<DataSource> =>
  request<DataSource>(`${DATA_SOURCE_BASE}/data-sources`, {
    method: 'POST',
    body: input,
  });

export const testDataSource = (input: { engine: string; dsn: string }): Promise<unknown> =>
  request(`${DATA_SOURCE_BASE}/data-sources:test`, {
    method: 'POST',
    body: input,
  });

export const updateDataSource = (
  dataSourceId: string,
  input: { name?: string; dsn?: string },
): Promise<DataSource> =>
  request<DataSource>(
    `${DATA_SOURCE_BASE}/data-sources/${encodeURIComponent(dataSourceId)}`,
    { method: 'PUT', body: input },
  );

export const deleteDataSource = (dataSourceId: string): Promise<unknown> =>
  request(`${DATA_SOURCE_BASE}/data-sources/${encodeURIComponent(dataSourceId)}`, {
    method: 'DELETE',
  });

/** 项目当前绑的数据源；没绑返回 null（那种项目回落到部署的默认库）。 */
export const getProjectDataSource = async (
  projectId: string,
): Promise<DataSource | null> => {
  const data = (await request<{ data_source?: DataSource | null }>(
    `/v1/analytics/projects/${encodeURIComponent(projectId)}/data-source`,
    { projectId },
  )) as { data_source?: DataSource | null } | null;
  return data?.data_source ?? null;
};

export const bindProjectDataSource = (
  projectId: string,
  dataSourceId: string,
): Promise<unknown> =>
  request(`/v1/analytics/projects/${encodeURIComponent(projectId)}/data-source`, {
    method: 'PUT',
    projectId,
    body: { data_source_id: dataSourceId },
  });


/** 上传的表格。 */
export interface UploadedTable {
  table: string;
  row_count: number;
  columns: string[];
}

export interface UploadColumn {
  name: string;
  source_name: string;
  type: string;
}

export interface UploadPreview {
  sheet: string;
  row_count?: number;
  columns?: UploadColumn[];
  /** 自动做了哪些改动。落库前要让用户过目。 */
  changes?: string[];
  /** 这张读不出来（空表、只有表头）。其余 sheet 照常可选。 */
  error?: { code: string; message: string };
}

export interface UploadInspection {
  sheets: string[];
  previews: UploadPreview[];
}

/** 一次看完所有 sheet——按 sheet 逐次调用会把同一个文件重传很多遍。 */
export const inspectUpload = (file: Blob): Promise<UploadInspection> =>
  request<UploadInspection>(`${DATA_SOURCE_BASE}/uploads:inspect`, {
    method: 'POST',
    file,
  });

export interface UploadOutcome {
  sheet: string;
  table: string;
  row_count?: number;
  error?: { code: string; message: string };
}

/** 一次导入多张。某张失败不影响已经成功的那几张，逐张带回结果。 */
export const commitUpload = (
  file: Blob,
  plan: { sheet: string; table: string }[],
): Promise<{ data_source_id: string; results: UploadOutcome[] }> =>
  request(`${DATA_SOURCE_BASE}/uploads:commit`, {
    method: 'POST',
    file,
    query: { plan: JSON.stringify(plan) },
  });

export const loadUpload = (
  file: Blob,
  input: { sheet: string; table: string; mode: 'append' | 'replace' },
): Promise<{ table: string; row_count: number; mode: string }> =>
  request(`${DATA_SOURCE_BASE}/uploads:load`, {
    method: 'POST',
    file,
    query: input,
  });

export const listUploads = async (): Promise<UploadedTable[]> => {
  const data = (await request<{ items?: UploadedTable[] }>(
    `${DATA_SOURCE_BASE}/uploads`,
  )) as { items?: UploadedTable[] } | null;
  return data?.items ?? [];
};

export const deleteUpload = (table: string): Promise<{ deleted: boolean }> =>
  request(`${DATA_SOURCE_BASE}/uploads/${encodeURIComponent(table)}`, {
    method: 'DELETE',
  });
