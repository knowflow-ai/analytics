import { describe, expect, it } from 'vitest';
import { AiModelingPanel } from './ai-modeling';
import { PublishPanel } from './publish';
import { SemanticCatalogPanel } from './semantic-catalog-panel';
import { WORKBENCH_STEPS, WorkbenchPage } from './index';
import { BusinessDictionaryPanel, TermEditorDialog } from './business-dictionary';
import publishSource from './publish.tsx?raw';

describe('complete-catalog workbench surfaces', () => {
  // 问数试问页已从共享 SPA 移除：开源版只有语义建模（数据源）一块，问数助手与报表是商业版产品面。
  it('exports the catalog, modeling and validation screens as one workflow', () => {
    expect([
      WorkbenchPage,
      SemanticCatalogPanel,
      BusinessDictionaryPanel,
      TermEditorDialog,
      AiModelingPanel,
      PublishPanel,
    ]).toSatisfy((items: unknown[]) => items.every((item) => typeof item === 'function'));
  });

  it('names the middle step for the user action rather than the resulting DTO', () => {
    expect(WORKBENCH_STEPS.map((step) => step.label)).toEqual([
      '数据源',
      '语义建模',
      '问数验证',
      // 发布后的回流入口：线上真实提问变成别名与术语。
      '问数反馈',
    ]);
  });

  it('keeps QueryScope out of ordinary natural-language asking surfaces', () => {
    expect(publishSource).not.toContain('setScopeId');
    expect(publishSource).not.toContain('<Select value={scopeId}');
  });

});
