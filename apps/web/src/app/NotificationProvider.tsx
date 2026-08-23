import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type PropsWithChildren,
} from "react";

export const MAX_NOTIFICATION_HISTORY = 100;

export type NotificationTone = "success" | "warning" | "error" | "info";

export type CenterNotification = {
  id: string;
  dedupeKey?: string;
  title: string;
  message?: string;
  tone: NotificationTone;
  href?: string;
  createdAt: string;
  read: boolean;
};

export type NotificationDraft = {
  dedupeKey?: string;
  title: string;
  message?: string;
  tone?: NotificationTone;
  href?: string;
  createdAt?: string;
};

type NotificationContextValue = {
  notifications: CenterNotification[];
  unreadCount: number;
  pushNotification: (notification: NotificationDraft) => void;
  markRead: (id: string) => void;
  markAllRead: () => void;
  dismissNotification: (id: string) => void;
  clearNotifications: () => void;
};

const noop = () => undefined;
const EMPTY_CONTEXT: NotificationContextValue = {
  notifications: [],
  unreadCount: 0,
  pushNotification: noop,
  markRead: noop,
  markAllRead: noop,
  dismissNotification: noop,
  clearNotifications: noop,
};
const NotificationContext = createContext<NotificationContextValue>(EMPTY_CONTEXT);
const SECRET_ASSIGNMENT =
  /\b(?:authorization|password|passwd|secret|token|api[-_ ]?key|private[-_ ]?key|cookie|set-cookie)\b\s*[:=]\s*\S+/i;
const BEARER_CREDENTIAL = /\bbearer\s+[a-z0-9._~+/=-]+/i;

export function safeNotificationText(value: unknown, limit = 240): string | undefined {
  if (typeof value !== "string") return undefined;
  const withoutControls = Array.from(value, (character) => {
    const code = character.charCodeAt(0);
    return code < 32 || code === 127 ? " " : character;
  }).join("");
  const compact = withoutControls.replace(/\s+/g, " ").trim();
  if (!compact || SECRET_ASSIGNMENT.test(compact) || BEARER_CREDENTIAL.test(compact))
    return undefined;
  return compact.slice(0, limit);
}

function notificationTimestamp(value?: string): string {
  const parsed = value ? new Date(value) : new Date();
  return Number.isFinite(parsed.getTime()) ? parsed.toISOString() : new Date().toISOString();
}

function internalHref(value?: string): string | undefined {
  return value?.startsWith("/") && !value.startsWith("//") ? value : undefined;
}

export function NotificationProvider({ children }: PropsWithChildren) {
  const [notifications, setNotifications] = useState<CenterNotification[]>([]);
  const pushNotification = useCallback((draft: NotificationDraft) => {
    const dedupeKey = safeNotificationText(draft.dedupeKey, 200);
    const title = safeNotificationText(draft.title, 100) ?? "Lab update";
    const message = safeNotificationText(draft.message, 240);
    setNotifications((current) => {
      if (dedupeKey && current.some((notification) => notification.dedupeKey === dedupeKey))
        return current;
      return [
        {
          id: crypto.randomUUID(),
          dedupeKey,
          title,
          message,
          tone: draft.tone ?? "info",
          href: internalHref(draft.href),
          createdAt: notificationTimestamp(draft.createdAt),
          read: false,
        },
        ...current,
      ].slice(0, MAX_NOTIFICATION_HISTORY);
    });
  }, []);
  const markRead = useCallback((id: string) => {
    setNotifications((current) =>
      current.map((notification) =>
        notification.id === id && !notification.read
          ? { ...notification, read: true }
          : notification,
      ),
    );
  }, []);
  const markAllRead = useCallback(() => {
    setNotifications((current) =>
      current.some((notification) => !notification.read)
        ? current.map((notification) => ({ ...notification, read: true }))
        : current,
    );
  }, []);
  const dismissNotification = useCallback((id: string) => {
    setNotifications((current) => current.filter((notification) => notification.id !== id));
  }, []);
  const clearNotifications = useCallback(() => {
    setNotifications((current) => (current.length ? [] : current));
  }, []);
  const unreadCount = useMemo(
    () => notifications.reduce((count, notification) => count + Number(!notification.read), 0),
    [notifications],
  );
  const value = useMemo(
    () => ({
      notifications,
      unreadCount,
      pushNotification,
      markRead,
      markAllRead,
      dismissNotification,
      clearNotifications,
    }),
    [
      clearNotifications,
      dismissNotification,
      markAllRead,
      markRead,
      notifications,
      pushNotification,
      unreadCount,
    ],
  );
  return <NotificationContext.Provider value={value}>{children}</NotificationContext.Provider>;
}

export function useNotifications() {
  return useContext(NotificationContext);
}
