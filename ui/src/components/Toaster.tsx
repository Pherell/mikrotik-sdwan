/**
 * Transient confirmation for things that change state.
 *
 * Mutating buttons previously did their work and said nothing. On a controller
 * that pushes configuration to routers, "did that go through?" is not a
 * question to leave to inference from a table refresh.
 *
 * Failures do not auto-dismiss. A success you missed costs nothing; an error
 * that vanished before you read it costs a support call.
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

type Tone = "ok" | "bad" | "info";

interface Toast {
  id: number;
  tone: Tone;
  text: string;
}

interface ToastApi {
  ok: (text: string) => void;
  bad: (text: string) => void;
  info: (text: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const DISMISS_AFTER: Record<Tone, number | null> = {
  ok: 4000,
  info: 6000,
  bad: null,
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);

  const push = useCallback((tone: Tone, text: string) => {
    const id = next.current++;
    setToasts((current) => [...current, { id, tone, text }]);
    const after = DISMISS_AFTER[tone];
    if (after !== null) {
      setTimeout(
        () => setToasts((current) => current.filter((t) => t.id !== id)),
        after,
      );
    }
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      ok: (text) => push("ok", text),
      bad: (text) => push("bad", text),
      info: (text) => push("info", text),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toaster" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.tone}`}>
            <span className="toast-text">{t.text}</span>
            <button
              className="ghost sm toast-close"
              onClick={() => setToasts((c) => c.filter((x) => x.id !== t.id))}
              aria-label="Dismiss"
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) throw new Error("useToast used outside ToastProvider");
  return api;
}
