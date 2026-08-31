import { Info, Loader2, TriangleAlert, X } from 'lucide-react';
import {
  createContext,
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

const VARIANTS: Record<Variant, string> = {
  primary: 'bg-blue-600 text-white hover:bg-blue-500 border-transparent shadow-sm',
  default: 'bg-white text-slate-700 hover:bg-slate-50 border-slate-200 shadow-sm',
  ghost: 'bg-transparent text-slate-600 hover:bg-slate-100 border-transparent',
  danger: 'bg-white text-red-600 hover:bg-red-50 border-red-200',
  dangerPrimary: 'bg-red-600 text-white hover:bg-red-500 border-transparent shadow-sm',
};
const SIZES: Record<Size, string> = {
  sm: 'h-7 px-2.5 text-xs gap-1',
  md: 'h-9 px-3.5 text-[13px] gap-1.5',
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
        'inline-flex items-center justify-center rounded-md border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50',
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

const CONTROL =
  'w-full rounded-md border border-slate-200 bg-white px-3 text-[13px] text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-100 disabled:bg-slate-50';

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cx(CONTROL, 'h-9', className)} {...rest} />;
}

export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cx(CONTROL, 'py-2 leading-relaxed', className)} {...rest} />;
}

export function Select({ className, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cx(CONTROL, 'h-9', className)} {...rest} />;
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
      <div className="mb-1 flex items-center gap-1 text-xs font-medium text-slate-600">
        {label}
        {tip && (
          <span className="group relative inline-flex">
            <Info className="h-3.5 w-3.5 cursor-help text-slate-400 hover:text-slate-600" />
            {/* 浮层是 group 的子节点:鼠标移进浮层仍算 hover,里面的按钮可点。 */}
            <span className="absolute left-0 top-full z-20 mt-1 hidden w-max max-w-[320px] rounded-md border border-slate-200 bg-white p-2.5 font-normal shadow-lg group-hover:block">
              {tip}
            </span>
          </span>
        )}
      </div>
      {children}
      {hint && <div className="mt-1 text-[11px] text-slate-400">{hint}</div>}
    </label>
  );
}

// --- Surfaces ---------------------------------------------------------------

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cx(
        'rounded-xl border border-slate-200 bg-white shadow-[0_1px_3px_rgba(15,23,42,0.04)]',
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
    slate: 'border-transparent bg-slate-100 text-slate-600',
    blue: 'border-transparent bg-blue-50 text-blue-700',
    green: 'border-transparent bg-emerald-50 text-emerald-700',
    amber: 'border-transparent bg-amber-50 text-amber-700',
    red: 'border-transparent bg-red-50 text-red-700',
    violet: 'border-transparent bg-violet-100 text-violet-700',
    sky: 'border-transparent bg-sky-100 text-sky-700',
  };
  const outline: Record<BadgeTone, string> = {
    slate: 'border-slate-300 bg-white text-slate-600',
    blue: 'border-blue-300 bg-white text-blue-700',
    green: 'border-emerald-300 bg-white text-emerald-700',
    amber: 'border-amber-300 bg-white text-amber-700',
    red: 'border-red-300 bg-white text-red-700',
    violet: 'border-violet-300 bg-white text-violet-700',
    sky: 'border-sky-300 bg-white text-sky-700',
  };
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium',
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
      <div className="text-sm font-medium text-slate-700">{title}</div>
      {hint && <div className="max-w-md text-xs text-slate-400">{hint}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-sm text-slate-400">
      <Loader2 className="h-4 w-4 animate-spin" />
      {label ?? '加载中…'}
    </div>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
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
        'fixed inset-0 flex items-center justify-center bg-slate-900/30 p-4',
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
        className={cx('flex max-h-[90vh] w-full flex-col rounded-xl bg-white shadow-xl', width, height)}
      >
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3">
          <div id={titleId} className="text-sm font-semibold text-slate-800">{title}</div>
          <button
            type="button"
            aria-label="关闭"
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            onClick={onClose}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-5 py-4">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-slate-100 px-5 py-3">
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
        <span className="mt-0.5 rounded-full bg-amber-50 p-2 text-amber-600">
          <TriangleAlert className="h-4 w-4" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1 text-xs leading-5 text-slate-600">
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

const ToastContext = createContext<(tone: Toast['tone'], message: string) => void>(() => {});

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
  const tones = {
    info: 'border-slate-200 bg-white text-slate-700',
    success: 'border-emerald-200 bg-emerald-50 text-emerald-800',
    error: 'border-red-200 bg-red-50 text-red-700',
  };
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed right-4 top-4 z-[60] flex w-80 flex-col gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={cx('rounded-md border px-3 py-2 text-xs shadow-md', tones[toast.tone])}
          >
            {toast.message}
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
