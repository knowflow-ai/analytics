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

/**
 * 任务型面板（问数验证、问数反馈、AI 建议核对）的可读宽度上限。
 *
 * **左对齐，不居中。** 宽度上限本身是对的（理由见下），但居中会让这两个 tab 的内容
 * 从屏幕中间开始，而数据源、语义建模两页是贴左的——切 tab 时左边缘跳来跳去（用户
 * 实测反馈）。上限保住可读性，左对齐保住一致性，两者不冲突。
 *
 * 宽屏下不加约束的后果，用户实测反馈：一条「待核对 · 销售金额 285126」左边贴着左墙、
 * 「数值正确 / 不对」贴着右墙，中间两千像素空白——眼睛得横扫一整行才认得出这两端是
 * 同一条记录。目录浏览与关系画布是密集型面板，继续吃满宽度（见
 * ANALYTICS_MAX_CONTENT_WIDTH_PX），这个约束只加在「一行一条、要逐条读」的面板上。
 */
export const ANALYTICS_TASK_PANEL_CLASS = 'w-full max-w-[1460px]';

/**
 * 找到真正能滚的那个祖先。
 *
 * 不能直接用 `scrollIntoView`：它会连 `overflow:hidden` 的祖先一起滚（hidden 在程序上
 * 依然是滚动容器），把一张本该固定的卡片内部顶上去，而用户没有任何办法滚回来。
 * 这里只认 `auto` / `scroll`，并且要求它真的有可滚内容。
 */
export function scrollableAncestor(node: HTMLElement): HTMLElement | null {
  let current: HTMLElement | null = node.parentElement;
  while (current) {
    const overflowY = window.getComputedStyle(current).overflowY;
    if (
      (overflowY === 'auto' || overflowY === 'scroll') &&
      current.scrollHeight > current.clientHeight
    ) {
      return current;
    }
    current = current.parentElement;
  }
  return null;
}
