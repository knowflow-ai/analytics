import { describe, expect, it } from 'vitest';
import askSource from '../ask.tsx?raw';
import { AskPage } from '../ask';
import { AiModelingPanel } from './ai-modeling';
import { PublishPanel } from './publish';
import { SemanticCatalogPanel } from './semantic-catalog-panel';
import { WORKBENCH_STEPS, WorkbenchPage } from './index';
import { BusinessDictionaryPanel, TermEditorDialog } from './business-dictionary';
import publishSource from './publish.tsx?raw';

describe('complete-catalog workbench surfaces', () => {
  it('exports the catalog, modeling, validation and ask screens as one workflow', () => {
    expect([
      WorkbenchPage,
      SemanticCatalogPanel,
      BusinessDictionaryPanel,
      TermEditorDialog,
      AiModelingPanel,
      PublishPanel,
      AskPage,
    ]).toSatisfy((items: unknown[]) => items.every((item) => typeof item === 'function'));
  });

  it('names the middle step for the user action rather than the resulting DTO', () => {
    expect(WORKBENCH_STEPS.map((step) => step.label)).toEqual([
      '数据源',
      '语义建模',
      '问数验证',
    ]);
  });

  it('keeps QueryScope out of ordinary natural-language asking surfaces', () => {
    expect(askSource).not.toContain('setDatasetId');
    expect(askSource).not.toContain('查询作用域');
    expect(publishSource).not.toContain('setScopeId');
    expect(publishSource).not.toContain('<Select value={scopeId}');
  });

  it('continues an old interpretation chip against the version that produced it', () => {
    expect(askSource).toContain('origin.target');
    expect(askSource).toContain("target.mode === 'release'");
  });
});
