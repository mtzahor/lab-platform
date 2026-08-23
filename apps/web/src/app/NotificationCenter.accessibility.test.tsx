import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { NotificationCenter } from "./NotificationCenter";
import { NotificationProvider, useNotifications } from "./NotificationProvider";

function SeedNotifications() {
  const { pushNotification } = useNotifications();
  return (
    <button
      type="button"
      onClick={() => {
        pushNotification({
          dedupeKey: "event-workflow",
          title: "Workflow Failed",
          message: "A workflow failed.",
          tone: "error",
          href: "/workflows",
          createdAt: "2026-08-23T09:15:00Z",
        });
        pushNotification({
          dedupeKey: "event-agent",
          title: "Agent Warning",
          message: "An Agent disconnected.",
          tone: "warning",
          href: "/agents",
          createdAt: "2026-08-23T09:16:00Z",
        });
      }}
    >
      Seed notifications
    </button>
  );
}

describe("NotificationCenter accessibility", () => {
  it("exposes unread state and supports read, dismiss, clear, and Escape focus restoration", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <NotificationProvider>
          <SeedNotifications />
          <NotificationCenter />
        </NotificationProvider>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Seed notifications" }));
    const bell = screen.getByRole("button", { name: "Notifications, 2 unread" });
    await user.click(bell);

    const dialog = screen.getByRole("dialog", { name: "Notifications" });
    expect(dialog).toHaveFocus();
    expect(dialog.querySelectorAll("time")).toHaveLength(2);
    expect(screen.getByText("A workflow failed.")).toBeVisible();
    expect(screen.getByText("An Agent disconnected.")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Mark Workflow Failed as read" }));
    expect(screen.getByRole("button", { name: "Notifications, 1 unread" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Dismiss Agent Warning" }));
    expect(screen.queryByText("An Agent disconnected.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Notifications, 0 unread" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Clear all" }));
    expect(screen.getByText("No notifications")).toBeVisible();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Notifications" })).not.toBeInTheDocument();
    expect(bell).toHaveFocus();
  });
});
