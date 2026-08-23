import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { Dialog } from "./ui";

function DialogHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open settings
      </button>
      <button type="button">Background action</button>
      <Dialog open={open} title="Edit settings" onClose={() => setOpen(false)}>
        <label>
          Setting
          <input autoFocus />
        </label>
        <button type="button">Save settings</button>
      </Dialog>
    </>
  );
}

describe("Dialog accessibility boundary", () => {
  it("inerts the background, contains Tab focus, and restores the invoker", async () => {
    const user = userEvent.setup();
    const { container } = render(<DialogHarness />);
    const invoker = screen.getByRole("button", { name: "Open settings" });
    const previousInert = container.inert;

    await user.click(invoker);

    const dialog = screen.getByRole("dialog", { name: "Edit settings" });
    const close = within(dialog).getByRole("button", { name: "Close dialog" });
    const input = within(dialog).getByRole("textbox", { name: "Setting" });
    const save = within(dialog).getByRole("button", { name: "Save settings" });
    expect(container.inert).toBe(true);
    expect(container).toHaveAttribute("aria-hidden", "true");
    expect(input).toHaveFocus();

    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(close).toHaveFocus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(save).toHaveFocus();
    await user.keyboard("{Tab}");
    expect(close).toHaveFocus();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(container.inert).toBe(previousInert);
    expect(container).not.toHaveAttribute("aria-hidden");
    expect(invoker).toHaveFocus();
  });
});
