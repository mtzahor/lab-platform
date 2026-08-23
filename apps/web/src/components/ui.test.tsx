import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiRecord } from "../api/client";
import { DataTable, Dialog, StatusBadge } from "./ui";

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
    const column = createColumnHelper<ApiRecord>();
    const columns: ColumnDef<ApiRecord, any>[] = [
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
  });
});
