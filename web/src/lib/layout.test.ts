import { describe, expect, it } from 'vitest';
import {
  ANALYTICS_CANVAS_BOTTOM_GAP_PX,
  ANALYTICS_FLUID_PANEL_CLASS,
  ANALYTICS_MAX_CONTENT_WIDTH_PX,
  PROJECT_CARD_MIN_WIDTH_PX,
  PROJECT_GRID_MAX_COLUMNS,
  availableCanvasHeight,
  projectGridTemplateColumns,
} from './layout';

describe('wide analytics layout', () => {
  it('uses the available wide viewport without making project cards too narrow', () => {
    expect(ANALYTICS_MAX_CONTENT_WIDTH_PX).toBe(2160);
    expect(PROJECT_CARD_MIN_WIDTH_PX).toBe(280);
    expect(PROJECT_GRID_MAX_COLUMNS).toBe(6);
    expect(projectGridTemplateColumns()).toBe(
      'repeat(auto-fill,minmax(min(100%,max(280px,calc((100% - 80px)/6))),1fr))',
    );

    const columnsAt = (contentWidth: number) => {
      const minWidth = Math.min(
        contentWidth,
        Math.max(
          PROJECT_CARD_MIN_WIDTH_PX,
          (contentWidth - 16 * (PROJECT_GRID_MAX_COLUMNS - 1)) /
            PROJECT_GRID_MAX_COLUMNS,
        ),
      );
      return Math.floor((contentWidth + 16) / (minWidth + 16));
    };
    expect(columnsAt(240)).toBe(1);
    expect(columnsAt(280)).toBe(1);
    expect(columnsAt(1360)).toBe(4);
    expect(columnsAt(1748)).toBe(5);
    expect(columnsAt(2120)).toBe(6);
  });

  it('lets the entity canvas consume tall-screen space without collapsing on short screens', () => {
    expect(ANALYTICS_CANVAS_BOTTOM_GAP_PX).toBe(24);
    expect(ANALYTICS_FLUID_PANEL_CLASS).toBe('min-h-0');
    expect(availableCanvasHeight(720, 180)).toBe(516);
    expect(availableCanvasHeight(889, 156)).toBe(709);
    expect(availableCanvasHeight(1440, 156)).toBe(1260);
    expect(availableCanvasHeight(500, 490)).toBe(0);
  });
});
