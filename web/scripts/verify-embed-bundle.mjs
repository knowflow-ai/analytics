#!/usr/bin/env node
/**
 * 嵌入产物守卫:构建配置一旦丢掉 --mode embedded,产物会静默退回 OSS 模式
 * (API 路径不重写、鉴权走错 token),而构建本身照样成功。实测踩过一次。
 */
import { existsSync, readFileSync } from 'node:fs';

const bundle = new URL('../dist-embedded/embed.js', import.meta.url);
const stylesheet = new URL('../dist-embedded/embed.css', import.meta.url);
const failures = [];

if (!existsSync(bundle)) {
  failures.push('dist-embedded/embed.js 不存在:先运行 npm run build:embedded');
} else {
  const code = readFileSync(bundle, 'utf8');
  if (!code.includes('/v1/analytics/core')) {
    failures.push('产物未注入门卫 API root:构建缺少 --mode embedded');
  }
  if (!/export\s*\{[^}]*\bmount\b/.test(code)) {
    failures.push('产物未导出 mount():宿主无法挂载');
  }
  if (code.includes('process.env.NODE_ENV')) {
    failures.push('产物残留 process.env:运行时会 ReferenceError');
  }
}
if (!existsSync(stylesheet)) failures.push('dist-embedded/embed.css 不存在');

if (failures.length) {
  console.error('嵌入产物校验失败:');
  for (const line of failures) console.error('  - ' + line);
  process.exit(1);
}
console.log('嵌入产物校验通过:门卫 API root、mount 导出、NODE_ENV 均正常。');
