import { BarChart3 } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { getOssSettings } from '@analytics/api/oss';
import { ApiError, setAccessToken } from '@analytics/api/client';
import { Button, Field, Input } from '@analytics/components/ui';

export function LoginPage({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    setAccessToken(password);
    try {
      // Any protected call proves the password; settings is the cheapest.
      await getOssSettings();
      onLoggedIn();
    } catch (caught) {
      setAccessToken('');
      setError(
        caught instanceof ApiError && caught.status === 401 ? '访问密码不正确' : `无法连接服务：${String(caught)}`,
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-full items-center justify-center bg-[rgb(var(--kf-fill-alter-rgb))] p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl border border-[var(--kf-border-secondary)] bg-[var(--kf-bg-container)] p-8 shadow-sm"
      >
        <div className="mb-6 flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-[var(--kf-primary)] to-[var(--kf-primary-hover)] text-[var(--kf-text-light-solid)]">
            <BarChart3 className="h-5 w-5" />
          </span>
          <div>
            <div className="text-base font-semibold text-[var(--kf-text)]">KnowFlow 智能问数</div>
            <div className="text-xs text-[var(--kf-text-tertiary)]">请输入访问密码</div>
          </div>
        </div>
        <Field label="访问密码">
          <Input
            type="password"
            autoFocus
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
        {error && <div className="mt-2 text-xs text-[var(--kf-error-text)]">{error}</div>}
        <Button
          type="submit"
          variant="primary"
          className="mt-5 w-full"
          loading={busy}
          disabled={!password}
        >
          进入
        </Button>
      </form>
    </div>
  );
}
