import { Bell, Check, CheckCircle2, CircleAlert, Info, Trash2, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { formatDate, formatRelative } from "../lib/format";
import { useNotifications, type CenterNotification } from "./NotificationProvider";

function NotificationIcon({ notification }: { notification: CenterNotification }) {
  const Icon =
    notification.tone === "success"
      ? CheckCircle2
      : notification.tone === "error" || notification.tone === "warning"
        ? CircleAlert
        : Info;
  return (
    <span className={`notification-icon notification-icon-${notification.tone}`}>
      <Icon size={17} aria-hidden />
    </span>
  );
}

export function NotificationCenter() {
  const {
    notifications,
    unreadCount,
    markRead,
    markAllRead,
    dismissNotification,
    clearNotifications,
  } = useNotifications();
  const [open, setOpen] = useState(false);
  const titleId = useId();
  const panelId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus({ preventScroll: true });
    const onMouseDown = (event: MouseEvent) => {
      const target = event.target;
      if (
        target instanceof Node &&
        !panelRef.current?.contains(target) &&
        !triggerRef.current?.contains(target)
      )
        setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus({ preventScroll: true });
    };
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div className="notification-center">
      <button
        ref={triggerRef}
        type="button"
        className="notification-button icon-button"
        aria-label={`Notifications, ${unreadCount} unread`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        <Bell size={19} aria-hidden />
        {unreadCount > 0 && (
          <span className="notification-count" aria-hidden>
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>
      <span className="sr-only" aria-live="polite">
        {unreadCount} unread {unreadCount === 1 ? "notification" : "notifications"}
      </span>
      {open && (
        <section
          ref={panelRef}
          id={panelId}
          className="notification-popover"
          role="dialog"
          aria-modal="false"
          aria-labelledby={titleId}
          tabIndex={-1}
        >
          <header className="notification-header">
            <div>
              <h2 id={titleId}>Notifications</h2>
              <p>{unreadCount ? `${unreadCount} unread` : "You are all caught up"}</p>
            </div>
            <div className="notification-header-actions">
              <button
                type="button"
                className="link-button"
                onClick={markAllRead}
                disabled={unreadCount === 0}
              >
                Mark all read
              </button>
              <button
                type="button"
                className="icon-button"
                aria-label="Close notifications"
                onClick={() => {
                  setOpen(false);
                  triggerRef.current?.focus({ preventScroll: true });
                }}
              >
                <X size={17} aria-hidden />
              </button>
            </div>
          </header>
          {notifications.length ? (
            <ul className="notification-list" aria-label="Recent notifications">
              {notifications.map((notification) => (
                <li
                  key={notification.id}
                  className={notification.read ? undefined : "notification-unread"}
                >
                  <NotificationIcon notification={notification} />
                  <div className="notification-content">
                    {notification.href ? (
                      <Link
                        to={notification.href}
                        onClick={() => {
                          markRead(notification.id);
                          setOpen(false);
                        }}
                      >
                        {notification.title}
                      </Link>
                    ) : (
                      <strong>{notification.title}</strong>
                    )}
                    {notification.message && <p>{notification.message}</p>}
                    <div className="notification-meta">
                      <time
                        dateTime={notification.createdAt}
                        title={formatDate(notification.createdAt)}
                      >
                        {formatRelative(notification.createdAt)}
                      </time>
                      {!notification.read && <span>Unread</span>}
                    </div>
                  </div>
                  <div className="notification-actions">
                    {!notification.read && (
                      <button
                        type="button"
                        className="icon-button"
                        aria-label={`Mark ${notification.title} as read`}
                        onClick={() => markRead(notification.id)}
                      >
                        <Check size={15} aria-hidden />
                      </button>
                    )}
                    <button
                      type="button"
                      className="icon-button"
                      aria-label={`Dismiss ${notification.title}`}
                      onClick={() => {
                        dismissNotification(notification.id);
                        queueMicrotask(() => panelRef.current?.focus({ preventScroll: true }));
                      }}
                    >
                      <X size={15} aria-hidden />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="notification-empty">
              <Bell size={24} aria-hidden />
              <strong>No notifications</strong>
              <span>Meaningful lab updates will appear here.</span>
            </div>
          )}
          <footer className="notification-footer">
            <button
              type="button"
              className="link-button"
              disabled={notifications.length === 0}
              onClick={clearNotifications}
            >
              <Trash2 size={14} aria-hidden /> Clear all
            </button>
          </footer>
        </section>
      )}
    </div>
  );
}
