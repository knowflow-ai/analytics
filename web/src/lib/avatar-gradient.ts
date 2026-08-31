/**
 * 按名字稳定映射到一组渐变色（v5 稿的多彩头像）。同一名字永远同色。
 * 用于无自定义 avatar 的知识库/聊天卡首字母头像 + 卡片顶部同色浅色带。
 */

// logo 实心渐变（8 色）。与下方 STRIPES / STRIPES_DARK 按 index 一一对应。
const GRADIENTS = [
  'linear-gradient(135deg,#2563eb,#3b82f6)', // 蓝
  'linear-gradient(135deg,#16a34a,#22c55e)', // 绿
  'linear-gradient(135deg,#7c3aed,#a855f7)', // 紫
  'linear-gradient(135deg,#ea580c,#f97316)', // 橙
  'linear-gradient(135deg,#db2777,#ec4899)', // 粉
  'linear-gradient(135deg,#0891b2,#06b6d4)', // 青
  'linear-gradient(135deg,#ca8a04,#eab308)', // 黄
  'linear-gradient(135deg,#dc2626,#ef4444)', // 红
];

// 卡片顶部浅色带（亮色）：与 logo 同色系的极浅底 → 白，营造淡淡主题氛围。
const STRIPES = [
  'linear-gradient(180deg,#eff6ff,#ffffff)', // 蓝
  'linear-gradient(180deg,#f0fdf4,#ffffff)', // 绿
  'linear-gradient(180deg,#faf5ff,#ffffff)', // 紫
  'linear-gradient(180deg,#fff7ed,#ffffff)', // 橙
  'linear-gradient(180deg,#fdf2f8,#ffffff)', // 粉
  'linear-gradient(180deg,#ecfeff,#ffffff)', // 青
  'linear-gradient(180deg,#fefce8,#ffffff)', // 黄
  'linear-gradient(180deg,#fef2f2,#ffffff)', // 红
];

// 卡片顶部浅色带（暗色）：同色相极低透明度着色 → 透明，避免亮底刺眼。
const STRIPES_DARK = [
  'linear-gradient(180deg,rgba(59,130,246,.16),transparent)', // 蓝
  'linear-gradient(180deg,rgba(34,197,94,.16),transparent)', // 绿
  'linear-gradient(180deg,rgba(168,85,247,.16),transparent)', // 紫
  'linear-gradient(180deg,rgba(249,115,22,.16),transparent)', // 橙
  'linear-gradient(180deg,rgba(236,72,153,.16),transparent)', // 粉
  'linear-gradient(180deg,rgba(6,182,212,.16),transparent)', // 青
  'linear-gradient(180deg,rgba(234,179,8,.16),transparent)', // 黄
  'linear-gradient(180deg,rgba(239,68,68,.16),transparent)', // 红
];

/** 名字 → 稳定 index（同名永远同 index，logo 与浅色带据此对齐）。 */
function indexOfName(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  }
  return hash % GRADIENTS.length;
}

export function avatarGradientOf(name: string): string {
  return GRADIENTS[indexOfName(name)];
}

/** 卡片顶部浅色带，与同名 logo 同色。isDark=true 走暗色渐变。 */
export function avatarStripeOf(name: string, isDark = false): string {
  return (isDark ? STRIPES_DARK : STRIPES)[indexOfName(name)];
}
