/**
 * Keep task-focused pages readable while allowing dense catalog/project
 * surfaces to use modern wide displays. 2160px yields at most six ~340px
 * project cards, instead of leaving ~450px blank on each side at 2.5K.
 */
export const ANALYTICS_MAX_CONTENT_WIDTH_PX = 2160;
export const PROJECT_CARD_MIN_WIDTH_PX = 280;
export const PROJECT_GRID_MAX_COLUMNS = 6;
export const ANALYTICS_CANVAS_BOTTOM_GAP_PX = 24;
export const ANALYTICS_FLUID_PANEL_CLASS = 'min-h-0';

export function availableCanvasHeight(
  viewportHeight: number,
  canvasTop: number,
  bottomGap = ANALYTICS_CANVAS_BOTTOM_GAP_PX,
): number {
  const available = viewportHeight - canvasTop - bottomGap;
  return Number.isFinite(available) ? Math.max(0, Math.floor(available)) : 0;
}

export function projectGridTemplateColumns(
  minCardWidth = PROJECT_CARD_MIN_WIDTH_PX,
  maxColumns = PROJECT_GRID_MAX_COLUMNS,
  gap = 16,
): string {
  const reservedGap = gap * (maxColumns - 1);
  return `repeat(auto-fill,minmax(min(100%,max(${minCardWidth}px,calc((100% - ${reservedGap}px)/${maxColumns}))),1fr))`;
}
