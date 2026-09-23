/** @type {import('tailwindcss').Config} */
// 与主应用 web/tailwind.config.js 同一套 Ant Design 刻度：开源版单独构建时也和嵌入后长得一样。
// 颜色一律写 --kf-* 令牌（src/theme/antd-tokens.css），不写 Tailwind 色号。
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      // 圆角 4/6/8（borderRadiusSM/borderRadius/borderRadiusLG），卡片、弹窗用 8
      borderRadius: {
        sm: 'var(--kf-radius-sm)',
        md: 'var(--kf-radius)',
        lg: 'var(--kf-radius-lg)',
        xl: 'var(--kf-radius-lg)',
      },
      // 字号 12/14/16/20/24/30，行高 20/22/24/28/32/38
      fontSize: {
        xs: ['12px', '20px'],
        sm: ['14px', '22px'],
        base: ['16px', '24px'],
        lg: ['20px', '28px'],
        xl: ['20px', '28px'],
        '2xl': ['24px', '32px'],
        '3xl': ['30px', '38px'],
      },
    },
  },
  plugins: [],
};
