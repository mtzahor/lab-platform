import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import {
  MAX_NOTIFICATION_HISTORY,
  NotificationProvider,
  useNotifications,
} from "./NotificationProvider";

function HistoryProbe() {
  const {
    notifications,
    unreadCount,
    pushNotification,
    markRead,
    dismissNotification,
    clearNotifications,
  } = useNotifications();
  const newest = notifications[0];
  return (
    <>
      <output data-testid="history-count">{notifications.length}</output>
      <output data-testid="unread-count">{unreadCount}</output>
      <ul>
        {notifications.map((notification) => (
          <li key={notification.id}>{notification.dedupeKey}</li>
        ))}
      </ul>
      <button
        type="button"
        onClick={() => {
          for (let index = 0; index < MAX_NOTIFICATION_HISTORY + 5; index += 1) {
            pushNotification({
              dedupeKey: `event-${index}`,
              title: `Update ${index}`,
              message: `Summary ${index}`,
            });
          }
        }}
      >
        Fill history
      </button>
      <button
        type="button"
        onClick={() =>
          pushNotification({
            dedupeKey: `event-${MAX_NOTIFICATION_HISTORY + 4}`,
            title: "Duplicate update",
          })
        }
      >
        Add duplicate
      </button>
      <button type="button" onClick={() => newest && markRead(newest.id)}>
        Read newest
      </button>
      <button type="button" onClick={() => newest && dismissNotification(newest.id)}>
        Dismiss newest
      </button>
      <button type="button" onClick={clearNotifications}>
        Clear history
      </button>
    </>
  );
}

describe("NotificationProvider history", () => {
  it("deduplicates by event key and retains only the newest bounded history", async () => {
    const user = userEvent.setup();
    render(
      <NotificationProvider>
        <HistoryProbe />
      </NotificationProvider>,
    );

    await user.click(screen.getByRole("button", { name: "Fill history" }));
    expect(screen.getByTestId("history-count")).toHaveTextContent(String(MAX_NOTIFICATION_HISTORY));
    expect(screen.getByTestId("unread-count")).toHaveTextContent(String(MAX_NOTIFICATION_HISTORY));
    expect(screen.getByText(`event-${MAX_NOTIFICATION_HISTORY + 4}`)).toBeVisible();
    expect(screen.queryByText("event-0")).not.toBeInTheDocument();
    expect(screen.queryByText("event-4")).not.toBeInTheDocument();
    expect(screen.getByText("event-5")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Add duplicate" }));
    expect(screen.getByTestId("history-count")).toHaveTextContent(String(MAX_NOTIFICATION_HISTORY));

    await user.click(screen.getByRole("button", { name: "Read newest" }));
    expect(screen.getByTestId("unread-count")).toHaveTextContent(
      String(MAX_NOTIFICATION_HISTORY - 1),
    );
    await user.click(screen.getByRole("button", { name: "Dismiss newest" }));
    expect(screen.getByTestId("history-count")).toHaveTextContent(
      String(MAX_NOTIFICATION_HISTORY - 1),
    );
    await user.click(screen.getByRole("button", { name: "Clear history" }));
    expect(screen.getByTestId("history-count")).toHaveTextContent("0");
  });
});
