import { createApiColumnHelper, type ApiColumnDef } from "../lib/table";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DataTable, Dialog, StatusBadge } from "./ui";

afterEach(cleanup);

describe("shared operational UI", () => {
  it("communicates status with text and an icon, not color alone", () => {
    render(<StatusBadge status="UNKNOWN" />);
    expect(screen.getByText("Unknown")).toBeVisible();
    expect(screen.getByText("Unknown").closest("span")?.querySelector("svg")).toBeTruthy();
  });

  it("closes a modal with Escape and exposes an accessible dialog name", () => {
    const close = vi.fn();
    render(
      <Dialog open title="Release simulation/bench-01" onClose={close}>
        Safe content
      </Dialog>,
    );
    expect(screen.getByRole("dialog", { name: "Release simulation/bench-01" })).toBeVisible();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(close).toHaveBeenCalledOnce();
  });

  it("paginates large data sets and keeps rows keyboard-operable", () => {
    const open = vi.fn();
    const column = createApiColumnHelper();
    const columns: ApiColumnDef[] = [
      column.accessor((row) => String(row.name), { id: "name", header: "Bench" }),
    ];
    const data = Array.from({ length: 1_000 }, (_, index) => ({
      id: String(index),
      name: `Bench ${index}`,
    }));
    render(
      <DataTable
        data={data}
        columns={columns}
        onRowClick={open}
        rowLabel={(row) => `Open ${row.name}`}
      />,
    );
    expect(screen.getAllByRole("row")).toHaveLength(26);
    const first = screen.getByRole("row", { name: "Open Bench 0" });
    first.focus();
    fireEvent.keyDown(first, { key: "Enter" });
    expect(open).toHaveBeenCalledWith(data[0]);
    expect(screen.getByText(/1,000 results/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText(/Page 2 of 40/)).toBeVisible();
    expect(screen.getByRole("row", { name: "Open Bench 25" })).toBeVisible();
    expect(screen.queryByRole("row", { name: "Open Bench 0" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(screen.getByRole("row", { name: "Open Bench 0" })).toBeVisible();
  });
});

describe("table sorting", () => {
  it("sorts text naturally and numbers numerically before pagination", () => {
    const column = createApiColumnHelper();
    const columns: ApiColumnDef[] = [
      column.accessor((row) => String(row.name), { id: "name", header: "Bench" }),
      column.accessor((row) => Number(row.count), { id: "count", header: "Count" }),
    ];
    render(
      <DataTable
        data={[
          { name: "Bench 10", count: 2 },
          { name: "Bench 2", count: 10 },
          { name: "Bench 1", count: 1 },
        ]}
        columns={columns}
        pageSize={2}
        rowLabel={(row) => String(row.name)}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Bench" }));
    expect(
      screen
        .getAllByRole("row")
        .slice(1)
        .map((row) => row.getAttribute("aria-label")),
    ).toEqual(["Bench 1", "Bench 2"]);
    fireEvent.click(screen.getByRole("button", { name: "Bench" }));
    expect(
      screen
        .getAllByRole("row")
        .slice(1)
        .map((row) => row.getAttribute("aria-label")),
    ).toEqual(["Bench 10", "Bench 2"]);
    fireEvent.click(screen.getByRole("button", { name: "Count" }));
    expect(
      screen
        .getAllByRole("row")
        .slice(1)
        .map((row) => row.getAttribute("aria-label")),
    ).toEqual(["Bench 2", "Bench 10"]);
  });
});
