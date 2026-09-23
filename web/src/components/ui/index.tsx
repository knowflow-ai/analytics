import { CircleCheck, CircleX, Info, Loader2, TriangleAlert, X } from 'lucide-react';
import {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';

export function cx(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(' ');
}

// --- Button -----------------------------------------------------------------

type Variant = 'primary' | 'default' | 'ghost' | 'danger' | 'dangerPrimary';
type Size = 'sm' | 'md';

// Ant Design Button：默认 32px、圆角 6；颜色全部读 --kf-* 令牌（与主应用同源）。
const VARIANTS: Record<Variant, string> = {
  primary:
    'bg-[var(--kf-primary)] text-[var(--kf-text-light-solid)] border-transparent shadow-[0_2px_0_rgb(var(--kf-primary-rgb)/0.1)] hover:bg-[var(--kf-primary-hover)] active:bg-[var(--kf-primary-active)]',
  default:
    'bg-[var(--kf-bg-container)] text-[var(--kf-text)] border-[var(--kf-border)] hover:text-[var(--kf-primary-hover)] hover:border-[var(--kf-primary-hover)]',
  ghost:
    'bg-transparent text-[var(--kf-text)] border-transparent hover:bg-[var(--kf-fill-tertiary)]',
  danger:
    'bg-[var(--kf-bg-container)] text-[var(--kf-error)] border-[var(--kf-error)] hover:text-[var(--kf-error-hover)] hover:border-[var(--kf-error-hover)]',
  dangerPrimary:
    'bg-[var(--kf-error)] text-[var(--kf-text-light-solid)] border-transparent shadow-[0_2px_0_rgb(var(--kf-error-rgb)/0.1)] hover:bg-[var(--kf-error-hover)]',
};
const SIZES: Record<Size, string> = {
  sm: 'h-6 rounded-sm px-[7px] text-sm gap-1',
  md: 'h-8 px-[15px] text-sm gap-2',
};

export function Button({
  variant = 'default',
  size = 'md',
  loading,
  icon,
  className,
  children,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  icon?: ReactNode;
}) {
  return (
    <button
      type="button"
      disabled={disabled || loading}
      className={cx(
        // shrink-0 + whitespace-nowrap:按钮在 flex 行里被旁边的长文本挤窄时，
        // 标签会换行并从固定高度里溢出（实测「新建」裂成两行）。按钮标签任何
        // 时候都不该换行——挤不下应该是旁边的文字让位，不是按钮变形。
        'inline-flex shrink-0 items-center justify-center whitespace-nowrap rounded-md border font-normal transition-colors disabled:cursor-not-allowed disabled:opacity-50',
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : icon}
      {children}
    </button>
  );
}

// --- Form controls ----------------------------------------------------------

// Ant Design Input：1px colorBorder、左右 11、hover/focus 主色边框 + 2px 外发光
const CONTROL =
  'w-full rounded-md border border-[var(--kf-border)] bg-[var(--kf-bg-container)] px-[11px] text-sm text-[var(--kf-text)] placeholder:text-[var(--kf-text-quaternary)] hover:border-[var(--kf-primary-hover)] focus:border-[var(--kf-primary)] focus:outline-none focus:shadow-[0_0_0_2px_rgb(var(--kf-primary-rgb)/0.1)] disabled:bg-[rgb(var(--kf-fill-tertiary-rgb))]';

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cx(CONTROL, 'h-8', className)} {...rest} />;
}

/** 转发 ref:表达式输入要读写光标位置,把名字插到光标处而不是追加到末尾。 */
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...rest }, ref) {
    return (
      <textarea ref={ref} className={cx(CONTROL, 'py-2 leading-relaxed', className)} {...rest} />
    );
  },
);

export function Select({ className, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cx(CONTROL, 'h-8', className)} {...rest} />;
}

export function Field({
  label,
  hint,
  tip,
  children,
}: {
  label: string;
  hint?: string;
  /** label 旁的 ⓘ 悬浮说明:规则、示例这类写之前该看的内容,不常驻占版面。 */
  tip?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <div className="mb-1 flex items-center gap-1 text-xs font-medium text-[var(--kf-text-secondary)]">
        {label}
        {tip && (
          <span className="group relative inline-flex">
            <Info className="h-3.5 w-3.5 cursor-help text-[var(--kf-text-tertiary)] hover:text-[var(--kf-text-secondary)]" />
            {/* 浮层是 group 的子节点:鼠标移进浮层仍算 hover,里面的按钮可点。 */}
            <span className="absolute left-0 top-full z-20 mt-1 hidden w-max max-w-[320px] rounded-md border border-[var(--kf-border-secondary)] bg-[var(--kf-bg-container)] p-2.5 font-normal shadow-lg group-hover:block">
              {tip}
            </span>
          </span>
        )}
      </div>
      {children}
      {hint && <div className="mt-1 text-xs text-[var(--kf-text-tertiary)]">{hint}</div>}
    </label>
  );
}

// --- Page heading -----------------------------------------------------------

/**
 * 页面标题区（Ant Design Pro PageContainer 形态）：标题 20/600 + 描述 14 次要色 + 右侧操作。
 * 与主应用 web/src/components/page-heading.tsx 同一套样式；本 SPA 需要单独构建（开源版），
 * 拿不到主仓组件，只能各放一份，改一边要同步改另一边。
 */
export function PageHeading({
  title,
  description,
  extra,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  extra?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cx('flex items-start justify-between gap-4', className)}>
      <div className="flex min-w-0 flex-col gap-1">
        <h1 className="truncate text-xl font-semibold text-[var(--kf-text)]">{title}</h1>
        {description && (
          <p className="text-sm text-[var(--kf-text-secondary)]">{description}</p>
        )}
      </div>
      {extra && <div className="flex shrink-0 items-center gap-2">{extra}</div>}
    </header>
  );
}

// --- Surfaces ---------------------------------------------------------------

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cx(
        // Ant Design Card：圆角 8、1px 分隔线色边框、无默认阴影
        'rounded-lg border border-[var(--kf-border-secondary)] bg-[var(--kf-bg-container)]',
        className,
      )}
    >
      {children}
    </div>
  );
}

export type BadgeTone = 'slate' | 'blue' | 'green' | 'amber' | 'red' | 'violet' | 'sky';
export type BadgeVariant = 'solid' | 'outline';

export function Badge({
  tone = 'slate',
  variant = 'solid',
  children,
}: {
  tone?: BadgeTone;
  variant?: BadgeVariant;
  children: ReactNode;
}) {
  // 实心也带一圈透明边框,和描边款高度一致,同排徽章不会错位。
  const solid: Record<BadgeTone, string> = {
    slate: 'border-transparent bg-[rgb(var(--kf-fill-tertiary-rgb))] text-[var(--kf-text-secondary)]',
    blue: 'border-transparent bg-[var(--kf-primary-bg)] text-[var(--kf-primary-active)]',
    green: 'border-transparent bg-[var(--kf-success-bg)] text-[var(--kf-success-text)]',
    amber: 'border-transparent bg-[var(--kf-warning-bg)] text-[var(--kf-warning-text)]',
    red: 'border-transparent bg-[var(--kf-error-bg)] text-[var(--kf-error-text)]',
    violet: 'border-transparent bg-violet-100 text-violet-700',
    sky: 'border-transparent bg-[var(--kf-primary-bg)] text-[var(--kf-primary-active)]',
  };
  const outline: Record<BadgeTone, string> = {
    slate: 'border-[var(--kf-border)] bg-[var(--kf-bg-container)] text-[var(--kf-text-secondary)]',
    blue: 'border-[var(--kf-primary-border)] bg-[var(--kf-bg-container)] text-[var(--kf-primary-active)]',
    green: 'border-[var(--kf-success-border)] bg-[var(--kf-bg-container)] text-[var(--kf-success-text)]',
    amber: 'border-[var(--kf-warning-border)] bg-[var(--kf-bg-container)] text-[var(--kf-warning-text)]',
    red: 'border-[var(--kf-error-border)] bg-[var(--kf-bg-container)] text-[var(--kf-error-text)]',
    violet: 'border-violet-300 bg-[var(--kf-bg-container)] text-violet-700',
    sky: 'border-[var(--kf-primary-border)] bg-[var(--kf-bg-container)] text-[var(--kf-primary-active)]',
  };
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium',
        (variant === 'outline' ? outline : solid)[tone],
      )}
    >
      {children}
    </span>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-16 text-center">
      <div className="text-sm font-medium text-[var(--kf-text)]">{title}</div>
      {hint && <div className="max-w-md text-xs text-[var(--kf-text-tertiary)]">{hint}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-sm text-[var(--kf-text-tertiary)]">
      <Loader2 className="h-4 w-4 animate-spin" />
      {label ?? '加载中…'}
    </div>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-[var(--kf-error-border)] bg-[var(--kf-error-bg)] px-3 py-2 text-xs text-[var(--kf-error-text)]">
      {message}
    </div>
  );
}

// --- Dialog -----------------------------------------------------------------

const DIALOG_FOCUSABLE = [
  'button:not([disabled])',
  'a[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export function wrappedDialogFocusIndex(
  activeIndex: number,
  focusableCount: number,
  backward: boolean,
): number | null {
  if (focusableCount === 0) return -1;
  if (activeIndex < 0) return backward ? focusableCount - 1 : 0;
  if (backward && activeIndex === 0) return focusableCount - 1;
  if (!backward && activeIndex === focusableCount - 1) return 0;
  return null;
}

export function Dialog({
  open,
  title,
  onClose,
  children,
  footer,
  width = 'max-w-lg',
  height,
  layer = 'default',
  role = 'dialog',
  inactive = false,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: string;
  /** 钉死高度(如 'h-[88vh]'):右栏内容随选中项变化时,弹窗不再跟着伸缩闪烁。 */
  height?: string;
  /** 确认框可叠在编辑框之上。 */
  layer?: 'default' | 'confirmation';
  role?: 'dialog' | 'alertdialog';
  /** 上层确认框打开时，让被遮住的父 Dialog 退出焦点和辅助技术导航。 */
  inactive?: boolean;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    if (!open || inactive) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [inactive, open, onClose]);
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    panel.inert = inactive;
    return () => {
      panel.inert = false;
    };
  }, [inactive, open]);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = window.requestAnimationFrame(() => {
      const panel = panelRef.current;
      const preferred = panel?.querySelector<HTMLElement>('[data-dialog-autofocus]');
      (preferred ?? panel)?.focus();
    });
    return () => {
      window.cancelAnimationFrame(frame);
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);
  if (!open) return null;
  return (
    <div
      className={cx(
        'fixed inset-0 flex items-center justify-center bg-[var(--kf-bg-mask)] p-4',
        layer === 'confirmation' ? 'z-[60]' : 'z-50',
      )}
      onMouseDown={(event) => {
        if (!inactive && event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role={role}
        aria-modal="true"
        aria-labelledby={titleId}
        aria-hidden={inactive || undefined}
        tabIndex={-1}
        onKeyDown={(event) => {
          if (inactive || event.key !== 'Tab') return;
          const panel = panelRef.current;
          if (!panel) return;
          const focusable = Array.from(panel.querySelectorAll<HTMLElement>(DIALOG_FOCUSABLE));
          const activeIndex = focusable.indexOf(document.activeElement as HTMLElement);
          const wrapped = wrappedDialogFocusIndex(activeIndex, focusable.length, event.shiftKey);
          if (wrapped === null) return;
          event.preventDefault();
          if (wrapped < 0) panel.focus();
          else focusable[wrapped]?.focus();
        }}
        className={cx('flex max-h-[90vh] w-full flex-col rounded-lg bg-[var(--kf-bg-elevated)] shadow-[var(--kf-shadow)]', width, height)}
      >
        {/* Ant Design Modal：标题 16/600，头尾不画分隔线，内边距 20/24 */}
        <div className="flex items-center justify-between px-6 pb-2 pt-5">
          <div id={titleId} className="text-base font-semibold text-[var(--kf-text)]">{title}</div>
          <button
            type="button"
            aria-label="关闭"
            className="rounded p-1 text-[var(--kf-text-tertiary)] hover:bg-[rgb(var(--kf-fill-tertiary-rgb))] hover:text-[var(--kf-text-secondary)]"
            onClick={onClose}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-6 py-3">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 px-6 pb-5 pt-3">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

export function ConfirmationDialog({
  open,
  title,
  description,
  confirmText = '确定',
  cancelText = '取消',
  danger = false,
  loading = false,
  onConfirm,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  description?: ReactNode;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  loading?: boolean;
  onConfirm: () => void;
  onClose: () => void;
  children?: ReactNode;
}) {
  const close = loading ? () => undefined : onClose;
  return (
    <Dialog
      open={open}
      title={title}
      onClose={close}
      width="max-w-md"
      layer="confirmation"
      role="alertdialog"
      footer={
        <>
          <Button autoFocus data-dialog-autofocus="" disabled={loading} onClick={onClose}>
            {cancelText}
          </Button>
          <Button
            variant={danger ? 'dangerPrimary' : 'primary'}
            loading={loading}
            onClick={onConfirm}
          >
            {confirmText}
          </Button>
        </>
      }
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 rounded-full bg-[var(--kf-warning-bg)] p-2 text-[var(--kf-warning-text)]">
          <TriangleAlert className="h-4 w-4" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1 text-xs leading-5 text-[var(--kf-text-secondary)]">
          {description && <div>{description}</div>}
          {children && <div className={description ? 'mt-3' : undefined}>{children}</div>}
        </div>
      </div>
    </Dialog>
  );
}

// --- Toast ------------------------------------------------------------------

interface Toast {
  id: number;
  tone: 'info' | 'success' | 'error';
  message: string;
}

/**
 * 默认值刻意**不是**静默空函数。
 *
 * 真实故障：宿主页面直接渲染 SPA 却漏挂 ToastProvider，于是整个嵌入版里所有的
 * 成功/失败提示全部消失——不报错、不警告，表现为「点了保存没有任何反应」，而
 * 请求其实成功了。一个什么都不做的默认值，让"忘了挂"变成了一种查不出来的故障。
 *
 * 现在它至少会在控制台喊一声，且把消息打出来，不至于一点线索都没有。
 */
const ToastContext = createContext<(tone: Toast['tone'], message: string) => void>(
  (tone, message) => {
    console.warn(
      `[knowflow-analytics] toast 未挂载 ToastProvider，消息被丢弃：[${tone}] ${message}`,
    );
  },
);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((tone: Toast['tone'], message: string) => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current, { id, tone, message }]);
    window.setTimeout(
      () => setToasts((current) => current.filter((item) => item.id !== id)),
      tone === 'error' ? 6000 : 3000,
    );
  }, []);
  // Ant Design message：顶部居中、白底，只有图标按类型着色（与主应用 ui/message 一致）
  const icons = {
    info: <Info className="h-4 w-4 shrink-0 text-[var(--kf-info)]" />,
    success: <CircleCheck className="h-4 w-4 shrink-0 text-[var(--kf-success)]" />,
    error: <CircleX className="h-4 w-4 shrink-0 text-[var(--kf-error)]" />,
  };
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed left-1/2 top-4 z-[60] flex -translate-x-1/2 flex-col items-center gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className="flex max-w-[480px] items-center gap-2 rounded-lg bg-[var(--kf-bg-elevated)] px-3 py-[9px] text-sm text-[var(--kf-text)] shadow-[var(--kf-shadow-secondary)]"
          >
            {icons[toast.tone]}
            <span>{toast.message}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const push = useContext(ToastContext);
  return useMemo(
    () => ({
      info: (message: string) => push('info', message),
      success: (message: string) => push('success', message),
      error: (message: string) => push('error', message),
    }),
    [push],
  );
}
