import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Braces, Network, Sigma, Wand2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { AnalyticsModelGraphLayout, AnalyticsRevision } from '@analytics/api/types';
import { ANALYTICS_FLUID_PANEL_CLASS } from '@analytics/lib/layout';
import { Button } from '@analytics/components/ui';
import { packedFallbackPositions, tidyLayout, unplacedModelIds } from '@analytics/lib/graph-layout';
import { CARDINALITY_LABELS } from '@analytics/lib/labels';
import { FIELD_ROLE_TEXT_CLASS, fieldRoleVisual } from '@analytics/lib/field-role';

interface ModelNodeData extends Record<string, unknown> {
  name: string;
  source: string;
  fieldCount: number;
  dimensionCount: number;
  metricCount: number;
  unclassifiedCount: number;
  fields: Array<{
    id: string;
    name: string;
    role: string;
    roleClass: string;
    pending: boolean;
    linkable: boolean;
  }>;
  onOpen: () => void;
}

type ModelNode = Node<ModelNodeData, 'model'>;
type RelationEdge = Edge<{ relationId: string }>;

function ModelNodeView({ data, selected }: NodeProps<ModelNode>) {
  return (
    <div
      role="button"
      tabIndex={0}
      className={`relative w-[252px] cursor-grab rounded-lg border bg-white shadow-[0_2px_10px_rgba(15,23,42,0.08)] active:cursor-grabbing ${
        selected ? 'border-blue-500 ring-2 ring-blue-100' : 'border-slate-200'
      }`}
      onClick={data.onOpen}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') data.onOpen();
      }}
    >
      <Handle
        id="target"
        type="target"
        position={Position.Left}
        className="!top-[30px] !h-4 !w-4 !border-2 !border-white !bg-blue-500"
      />
      <div className="rounded-t-lg border-b border-slate-100 bg-slate-50/70 px-3 py-2.5 text-left">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
          <Network className="h-4 w-4 text-blue-500" />
          <span className="min-w-0 flex-1 truncate">{data.name}</span>
        </div>
        <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-slate-500">
          <span className="min-w-0 truncate">{data.source}</span>
          <span className="shrink-0">{data.fieldCount} 字段</span>
        </div>
      </div>
      <div className="divide-y divide-slate-100">
        {data.fields.map((field) => (
          <div
            key={field.id}
            className="relative flex items-center justify-between bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
          >
            {field.linkable && (
              <Handle
                id={`field-target:${field.id}`}
                type="target"
                position={Position.Left}
                className="!h-3 !w-3 !border-2 !border-white !bg-emerald-500"
              />
            )}
            <span className="min-w-0 flex-1 truncate">{field.name}</span>
            <span className={`ml-2 shrink-0 ${field.roleClass}`}>{field.role}</span>
            {field.linkable && (
              <Handle
                id={`field-source:${field.id}`}
                type="source"
                position={Position.Right}
                className="!h-3 !w-3 !border-2 !border-white !bg-emerald-500"
              />
            )}
          </div>
        ))}
      </div>
      <div className="flex items-center gap-3 rounded-b-lg border-t border-slate-100 bg-slate-50 px-3 py-1.5 text-[11px] text-slate-500">
        <span className="flex items-center gap-1">
          <Braces className="h-3 w-3 text-blue-500" />
          {data.dimensionCount} 维度
        </span>
        <span className="flex items-center gap-1">
          <Sigma className="h-3 w-3 text-emerald-600" />
          {data.metricCount} 指标
        </span>
        {data.unclassifiedCount > 0 && (
          <span className="ml-auto text-blue-600">{data.unclassifiedCount} 待确认</span>
        )}
      </div>
      <Handle
        id="source"
        type="source"
        position={Position.Right}
        className="!top-[30px] !h-4 !w-4 !border-2 !border-white !bg-blue-500"
      />
    </div>
  );
}

const nodeTypes = { model: ModelNodeView };

function buildEdges(
  spec: AnalyticsRevision['semantic_spec'],
  unconfirmed: ReadonlySet<string>,
): RelationEdge[] {
  const fieldModel = new Map(spec.fields.map((field) => [field.id, field.model_id]));
  return spec.relations.flatMap((relation) => {
    const pending = unconfirmed.has(relation.id);
    const color = pending ? '#d97706' : relation.cardinality === 'many_to_many' ? '#dc2626' : '#3b82f6';
    const label = pending ? '待确认基数' : CARDINALITY_LABELS[relation.cardinality];
    const edge = (id: string, sourceHandle: string, targetHandle: string, showLabel: boolean): RelationEdge => ({
      id,
      source: relation.left_model_id,
      target: relation.right_model_id,
      sourceHandle,
      targetHandle,
      data: { relationId: relation.id },
      label: showLabel ? label : undefined,
      type: 'smoothstep',
      markerEnd: { type: MarkerType.ArrowClosed, color },
      style: { stroke: color, strokeWidth: 2, strokeDasharray: pending ? '6 4' : undefined },
      labelStyle: { fill: color, fontSize: 12, fontWeight: 600 },
      labelBgStyle: { fill: '#ffffff', fillOpacity: 0.95 },
      labelBgPadding: [6, 3] as [number, number],
      labelBgBorderRadius: 10,
    });
    const anchored =
      relation.conditions.length > 0 &&
      relation.conditions.every(
        (condition) =>
          fieldModel.get(condition.left_field_id) === relation.left_model_id &&
          fieldModel.get(condition.right_field_id) === relation.right_model_id,
      );
    if (!anchored) return [edge(relation.id, 'source', 'target', true)];
    return relation.conditions.map((condition, index) =>
      edge(
        `${relation.id}:${index}`,
        `field-source:${condition.left_field_id}`,
        `field-target:${condition.right_field_id}`,
        index === 0,
      ),
    );
  });
}

export interface ModelGraphProps {
  revision: AnalyticsRevision;
  unconfirmedRelationIds: ReadonlySet<string>;
  layout: AnalyticsModelGraphLayout;
  onOpenModel: (modelId: string) => void;
  onOpenRelation: (relationId: string) => void;
  onCreateRelation: (fromModelId: string, toModelId: string, fromFieldId?: string, toFieldId?: string) => void;
  onSaveLayout: (
    positions: AnalyticsModelGraphLayout['positions'],
    viewport: AnalyticsModelGraphLayout['viewport'],
  ) => void;
}

export function ModelGraph({
  revision,
  unconfirmedRelationIds,
  layout,
  onOpenModel,
  onOpenRelation,
  onCreateRelation,
  onSaveLayout,
}: ModelGraphProps) {
  const spec = revision.semantic_spec;
  const savedPositions = useMemo(
    () => new Map(layout.positions.map((item) => [item.model_id, item])),
    [layout.positions],
  );
  const fieldCounts = useMemo(() => {
    const counts = new Map<string, number>();
    spec.fields.forEach((field) => counts.set(field.model_id, (counts.get(field.model_id) ?? 0) + 1));
    return counts;
  }, [spec.fields]);

  // ELK 算完前的一瞬间用它,不能压盖(固定行距正是旧占位格重叠的原因)。
  const fallbackPositions = useMemo(
    () => packedFallbackPositions({ models: spec.models, fieldCounts }),
    [fieldCounts, spec.models],
  );

  const graphNodes = useMemo<ModelNode[]>(
    () =>
      spec.models.map((model) => {
        const fields = spec.fields.filter((field) => field.model_id === model.id);
        return {
          id: model.id,
          type: 'model',
          position: savedPositions.get(model.id) ?? fallbackPositions[model.id],
          data: {
            name: model.name,
            source: [model.schema_name, model.table].filter(Boolean).join('.'),
            fieldCount: fields.length,
            dimensionCount: spec.dimensions.filter((d) => d.model_id === model.id).length,
            metricCount: spec.metrics.filter((m) => m.model_id === model.id).length,
            unclassifiedCount: fields.filter((field) => field.kind === 'field').length,
            fields: fields.map((field) => ({
              id: field.id,
              name: field.name,
              role: fieldRoleVisual(field).label,
              roleClass: FIELD_ROLE_TEXT_CLASS[fieldRoleVisual(field).tone],
              pending: field.kind === 'field',
              // Only identifiers may anchor a relation; the contract rejects others.
              linkable: field.kind === 'identifier',
            })),
            onOpen: () => onOpenModel(model.id),
          },
        };
      }),
    [fallbackPositions, onOpenModel, savedPositions, spec],
  );
  const [nodes, setNodes, onNodesChange] = useNodesState(graphNodes);
  useEffect(() => setNodes(graphNodes), [graphNodes, setNodes]);
  const edges = useMemo(() => buildEdges(spec, unconfirmedRelationIds), [spec, unconfirmedRelationIds]);

  const flowRef = useRef<ReactFlowInstance<ModelNode, RelationEdge> | null>(null);
  const [tidying, setTidying] = useState(false);
  const saveTimer = useRef<number>();
  const scheduleSave = useCallback(() => {
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      const instance = flowRef.current;
      if (!instance) return;
      onSaveLayout(
        instance.getNodes().map((node) => ({ model_id: node.id, x: node.position.x, y: node.position.y })),
        instance.getViewport(),
      );
    }, 800);
  }, [onSaveLayout]);
  useEffect(() => () => window.clearTimeout(saveTimer.current), []);

  const tidy = useCallback(
    async ({ immediate = false }: { immediate?: boolean } = {}) => {
      setTidying(true);
      try {
        const positions = await tidyLayout({ models: spec.models, relations: spec.relations, fieldCounts });
        setNodes((current) =>
          current.map((node) => (positions[node.id] ? { ...node, position: positions[node.id] } : node)),
        );
        // 首次排布直接落库:靠 800ms 防抖的话,用户在这之前离开画布就白排了,
        // 下次进来又是一张没排过的图。坐标用刚算出的这份,不去读画布实例——
        // setTimeout(0) 时它未必已经跟上这次 setNodes。
        if (immediate) {
          window.clearTimeout(saveTimer.current);
          onSaveLayout(
            spec.models
              .filter((model) => positions[model.id])
              .map((model) => ({ model_id: model.id, ...positions[model.id] })),
            flowRef.current?.getViewport() ?? { x: 0, y: 0, zoom: 1 },
          );
        }
        window.setTimeout(() => {
          flowRef.current?.fitView({ padding: 0.18, maxZoom: 1 });
          if (!immediate) scheduleSave();
        }, 0);
      } finally {
        setTidying(false);
      }
    },
    [fieldCounts, onSaveLayout, scheduleSave, setNodes, spec.models, spec.relations],
  );

  // 有模型还没有坐标就整理一次并落库:新建项目、或导入新表后的第一次打开。
  // 判断依据是服务端真实存了哪些坐标——它不再为缺坐标的模型编造占位格。
  const autoTidiedFor = useRef<string>('');
  const unplaced = unplacedModelIds(spec.models, new Set(savedPositions.keys())).join(',');
  useEffect(() => {
    if (!spec.models.length || !unplaced) return;
    if (autoTidiedFor.current === unplaced) return;
    autoTidiedFor.current = unplaced;
    void tidy({ immediate: true });
  }, [spec.models.length, tidy, unplaced]);

  const connect = (connection: Connection) => {
    if (!connection.source || !connection.target || connection.source === connection.target) return;
    const from = connection.sourceHandle?.startsWith('field-source:')
      ? connection.sourceHandle.slice('field-source:'.length)
      : undefined;
    const to = connection.targetHandle?.startsWith('field-target:')
      ? connection.targetHandle.slice('field-target:'.length)
      : undefined;
    if (Boolean(from) !== Boolean(to)) return;
    onCreateRelation(connection.source, connection.target, from, to);
  };

  return (
    <div className={`h-full bg-slate-50 ${ANALYTICS_FLUID_PANEL_CLASS}`}>
      <ReactFlow
        onInit={(instance) => {
          flowRef.current = instance;
        }}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.18, maxZoom: 1 }}
        defaultViewport={layout.viewport}
        minZoom={0.2}
        maxZoom={1.5}
        onNodesChange={onNodesChange}
        onNodeDragStop={scheduleSave}
        onMoveEnd={scheduleSave}
        onConnect={connect}
        isValidConnection={(connection) =>
          Boolean(connection.source && connection.target && connection.source !== connection.target) &&
          Boolean(connection.sourceHandle?.startsWith('field-source:')) ===
            Boolean(connection.targetHandle?.startsWith('field-target:'))
        }
        onEdgeClick={(_, edge) => onOpenRelation(edge.data?.relationId ?? edge.id)}
      >
        <Background gap={20} size={1} />
        <MiniMap pannable zoomable style={{ width: 120, height: 80 }} maskColor="rgba(248,250,252,0.72)" />
        <Controls />
        <Panel position="bottom-right">
          <div className="flex items-center gap-3 rounded-md border border-slate-200 bg-white/95 px-2.5 py-1.5 text-[11px] text-slate-500 shadow-sm">
            <span className="flex items-center gap-1.5">
              <span className="inline-block w-4 border-t-2 border-blue-500" />已确认基数
            </span>
            <span className="flex items-center gap-1.5">
              <span className="inline-block w-4 border-t-2 border-dashed border-amber-500" />待确认基数
            </span>
            <span className="flex items-center gap-1.5">
              <span className="inline-block w-4 border-t-2 border-red-600" />多对多
            </span>
          </div>
        </Panel>
        <Panel position="top-right">
          <div className="flex items-center gap-2">
            <span className="rounded-md border border-slate-200 bg-white/95 px-2 py-1 text-[11px] text-slate-500 shadow-sm">
              拖动绿色标识连接点创建关系 · 点击连线编辑
            </span>
            <Button size="sm" icon={<Wand2 className="h-3.5 w-3.5" />} loading={tidying} onClick={() => void tidy()}>
              规整
            </Button>
          </div>
        </Panel>
      </ReactFlow>
    </div>
  );
}
