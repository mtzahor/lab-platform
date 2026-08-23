import { CheckCircle2, CircleAlert, Info, X } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useState,
  type PropsWithChildren,
  type ReactNode,
} from "react";

export type ToastTone = "success" | "error" | "info";
type Toast = { id: string; title: string; message?: ReactNode; tone: ToastTone; href?: string };
type ToastContextValue = { notify: (toast: Omit<Toast, "id">) => void };

const ToastContext = createContext<ToastContextValue | undefined>(undefined);

export function ToastProvider({ children }: PropsWithChildren) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = useCallback(
    (id: string) => setToasts((current) => current.filter((toast) => toast.id !== id)),
    [],
  );
  const notify = useCallback(
    (toast: Omit<Toast, "id">) => {
      const id = crypto.randomUUID();
      setToasts((current) => [...current.slice(-3), { ...toast, id }]);
      window.setTimeout(() => dismiss(id), toast.tone === "error" ? 8_000 : 5_000);
    },
    [dismiss],
  );
  return (
    <ToastContext.Provider value={{ notify }}>
      {children}
      <div className="toast-region" role="region" aria-label="Notifications" aria-live="polite">
        {toasts.map((toast) => {
          const Icon =
            toast.tone === "success" ? CheckCircle2 : toast.tone === "error" ? CircleAlert : Info;
          const body = (
            <>
              <Icon size={18} aria-hidden />
              <div>
                <strong>{toast.title}</strong>
                {toast.message && <span>{toast.message}</span>}
              </div>
            </>
          );
          return (
            <div className={`toast toast-${toast.tone}`} key={toast.id}>
              {toast.href ? <a href={toast.href}>{body}</a> : body}
              <button
                className="icon-button"
                aria-label="Dismiss notification"
                onClick={() => dismiss(toast.id)}
              >
                <X size={16} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) throw new Error("useToast must be used inside ToastProvider");
  return context;
}
