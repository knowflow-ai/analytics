import { useLayoutEffect, useRef, useState } from 'react';

import { availableCanvasHeight } from '@analytics/lib/layout';

/**
 * 从这个元素的顶边一直用到视口底部的可用高度。
 *
 * 建模工作台四个 tab（数据源、语义建模、问数验证、问数反馈）共用一个容器。不给它固定
 * 高度的话，容器跟着各自的内容撑开——切 tab 时整块面板忽上忽下，用户实测反馈就是这个。
 * 关系画布本来就按这个公式撑满视口，四个 tab 用同一个算法，切换时框不动。
 *
 * 观察范围是三层祖先加父节点的子元素变化：面板顶边会因为上方的提示条出现/消失而移动，
 * 只在 resize 时测量会漏掉这些。
 */
export function useAvailableCanvasHeight(enabled: boolean, measurementKey: string) {
  const ref = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number>();

  useLayoutEffect(() => {
    if (!enabled) return;
    let frame = 0;
    const measure = () => {
      const node = ref.current;
      if (!node) return;
      const visualViewport = window.visualViewport;
      const next = availableCanvasHeight(
        visualViewport?.height ?? window.innerHeight,
        node.getBoundingClientRect().top - (visualViewport?.offsetTop ?? 0),
      );
      setHeight((current) => (current === next ? current : next));
    };
    const scheduleMeasure = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(measure);
    };

    measure();
    window.addEventListener('resize', scheduleMeasure);
    window.visualViewport?.addEventListener('resize', scheduleMeasure);
    const observer =
      typeof ResizeObserver === 'undefined'
        ? null
        : new ResizeObserver(scheduleMeasure);
    const parent = ref.current?.parentElement;
    let ancestor = parent;
    for (let depth = 0; ancestor && depth < 3; depth += 1) {
      observer?.observe(ancestor);
      ancestor = ancestor.parentElement;
    }
    const mutationObserver =
      typeof MutationObserver === 'undefined'
        ? null
        : new MutationObserver(scheduleMeasure);
    if (parent) mutationObserver?.observe(parent, { childList: true });
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener('resize', scheduleMeasure);
      window.visualViewport?.removeEventListener('resize', scheduleMeasure);
      observer?.disconnect();
      mutationObserver?.disconnect();
    };
  }, [enabled, measurementKey]);

  return { ref, height };
}
