import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Inbox,
  LoaderCircle,
  MoreHorizontal,
  Search,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type PropsWithChildren,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { apiErrorDetails, errorMessage, type ApiRecord } from "../api/client";
import { titleCase } from "../lib/format";
import { statusIcon, statusTone } from "../lib/status";

export function Button({
  variant = "primary",
  icon: Icon,
  children,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  icon?: LucideIcon;
}) {
  return (
    <button className={`button button-${variant} ${className}`} {...props}>
      {Icon && <Icon size={16} aria-hidden />}
      {children}
    </button>
  );
}

export function StatusBadge({ status, label }: { status?: string; label?: string }) {
  const Icon = statusIcon(status);
  return (
    <span className={`status-badge tone-${statusTone(status)}`}>
      <Icon size={13} aria-hidden />
      {label ?? titleCase(status)}
    </span>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        {description && <div className="page-description">{description}</div>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function Panel({
  title,
  description,
  action,
  children,
  className = "",
}: PropsWithChildren<{
  title?: string;
  description?: string;
  action?: ReactNode;
  className?: string;
}>) {
  return (
    <section className={`panel ${className}`}>
      {(title || action) && (
        <header className="panel-header">
          <div>
            {title && <h2>{title}</h2>}
            {description && <p>{description}</p>}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function MetricCard({
  label,
  value,
  hint,
  icon: Icon,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon: LucideIcon;
  tone?: "neutral" | "positive" | "warning" | "negative" | "info";
}) {
  return (
    <div className={`metric-card metric-${tone}`}>
      <div className="metric-icon">
        <Icon size={19} aria-hidden />
      </div>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        {hint && <small>{hint}</small>}
      </div>
    </div>
  );
}

export function SearchField({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className={`search-field ${className}`}>
      <Search size={16} aria-hidden />
      <span className="sr-only">Search</span>
      <input type="search" {...props} />
    </label>
  );
}

export function Field({
  label,
  error,
  hint,
  children,
  required,
}: PropsWithChildren<{ label: string; error?: string; hint?: string; required?: boolean }>) {
  return (
    <label className="field">
      <span>
        {label}
        {required && <b aria-hidden> *</b>}
      </span>
      {children}
      {hint && !error && <small>{hint}</small>}
      {error && (
        <small className="field-error" role="alert">
          {error}
        </small>
      )}
    </label>
  );
}

export function EmptyState({
  title,
  description,
  icon: Icon = Inbox,
  action,
}: {
  title: string;
  description: string;
  icon?: LucideIcon;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span>
        <Icon size={22} aria-hidden />
      </span>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function LoadingState({ label = "Loading" }: { label?: string }) {
  return (
    <div className="loading-state" role="status">
      <LoaderCircle className="spin" size={18} aria-hidden /> {label}…
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const details = apiErrorDetails(error);
  return (
    <div className="error-state" role="alert">
      <AlertCircle size={20} aria-hidden />
      <div>
        <strong>Couldn’t load this data</strong>
        <p>{errorMessage(error)}</p>
        {(details.code || details.requestId) && (
          <details>
            <summary>Technical details</summary>
            <code>
              {details.code ?? "UNKNOWN"}
              {details.requestId ? ` · Request ${details.requestId}` : ""}
            </code>
          </details>
        )}
      </div>
      {retry && (
        <Button variant="secondary" onClick={retry}>
          Try again
        </Button>
      )}
    </div>
  );
}

export function DataTable({
  data,
  columns,
  rowLabel,
  pageSize = 25,
  onRowClick,
}: {
  data: ApiRecord[];
  columns: ColumnDef<ApiRecord, any>[];
  rowLabel?: (row: ApiRecord) => string;
  pageSize?: number;
  onRowClick?: (row: ApiRecord) => void;
}) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize } },
  });

  return (
    <div className="data-table-wrap">
      <table className="data-table">
        <thead>
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => {
                const sorted = header.column.getIsSorted();
                return (
                  <th key={header.id}>
                    {header.isPlaceholder ? null : header.column.getCanSort() ? (
                      <button onClick={header.column.getToggleSortingHandler()}>
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {sorted === "asc" ? (
                          <ArrowUp size={13} />
                        ) : sorted === "desc" ? (
                          <ArrowDown size={13} />
                        ) : (
                          <ArrowUpDown size={13} />
                        )}
                      </button>
                    ) : (
                      flexRender(header.column.columnDef.header, header.getContext())
                    )}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr
              key={row.id}
              className={onRowClick ? "clickable-row" : undefined}
              tabIndex={onRowClick ? 0 : undefined}
              aria-label={rowLabel?.(row.original)}
              onClick={onRowClick ? () => onRowClick(row.original) : undefined}
              onKeyDown={
                onRowClick
                  ? (event) => {
                      if (event.key === "Enter") onRowClick(row.original);
                    }
                  : undefined
              }
            >
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {table.getPageCount() > 1 && (
        <div className="table-pagination">
          <span>
            Page {table.getState().pagination.pageIndex + 1} of {table.getPageCount()} ·{" "}
            {data.length.toLocaleString()} results
          </span>
          <div>
            <Button
              variant="ghost"
              onClick={() => table.previousPage()}
              disabled={!table.getCanPreviousPage()}
            >
              Previous
            </Button>
            <Button
              variant="ghost"
              onClick={() => table.nextPage()}
              disabled={!table.getCanNextPage()}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export function Dialog({
  open,
  title,
  description,
  children,
  onClose,
  size = "normal",
}: PropsWithChildren<{
  open: boolean;
  title: string;
  description?: string;
  onClose: () => void;
  size?: "normal" | "wide";
}>) {
  const titleId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  const invokerRef = useRef<HTMLElement | undefined>(undefined);
  const wasOpenRef = useRef(false);
  if (open && !wasOpenRef.current) {
    invokerRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : undefined;
  }
  wasOpenRef.current = open;
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);
  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    const invoker = invokerRef.current;
    const background = Array.from(document.body.children)
      .filter((element) => element !== dialog?.parentElement)
      .map((element) => ({
        element: element as HTMLElement,
        inert: (element as HTMLElement).inert,
        ariaHidden: element.getAttribute("aria-hidden"),
      }));
    for (const item of background) {
      item.element.inert = true;
      item.element.setAttribute("aria-hidden", "true");
    }
    if (!dialog?.contains(document.activeElement)) closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || event.defaultPrevented || !dialog) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [contenteditable="true"], [tabindex]:not([tabindex="-1"])',
        ),
      ).filter(
        (element) =>
          !element.hidden &&
          element.getAttribute("aria-hidden") !== "true" &&
          !element.closest("[hidden], [inert]"),
      );
      const first = focusable.at(0);
      const last = focusable.at(-1);
      const activeElement = document.activeElement;
      if (!first || !last) {
        event.preventDefault();
        dialog.focus();
      } else if (event.shiftKey && (activeElement === first || !dialog.contains(activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (activeElement === last || !dialog.contains(activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    const bodyAlreadyLocked = document.body.classList.contains("dialog-open");
    document.body.classList.add("dialog-open");
    return () => {
      document.removeEventListener("keydown", onKey);
      for (const item of background) {
        item.element.inert = item.inert;
        if (item.ariaHidden === null) item.element.removeAttribute("aria-hidden");
        else item.element.setAttribute("aria-hidden", item.ariaHidden);
      }
      if (!bodyAlreadyLocked) document.body.classList.remove("dialog-open");
      if (invoker?.isConnected) invoker.focus({ preventScroll: true });
    };
  }, [open]);
  if (!open) return null;
  return createPortal(
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className={`dialog dialog-${size}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <header>
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && <p>{description}</p>}
          </div>
          <button
            ref={closeRef}
            className="icon-button"
            onClick={onClose}
            aria-label="Close dialog"
          >
            <X size={18} />
          </button>
        </header>
        <div className="dialog-content">{children}</div>
      </section>
    </div>,
    document.body,
  );
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  tone = "danger",
  busy,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmLabel: string;
  tone?: "danger" | "primary";
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog open={open} onClose={onClose} title={title}>
      <p className="confirm-message">{message}</p>
      <div className="dialog-actions">
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Keep unchanged
        </Button>
        <Button variant={tone} onClick={onConfirm} disabled={busy}>
          {busy ? "Working…" : confirmLabel}
        </Button>
      </div>
    </Dialog>
  );
}

export function ChipList({ values, limit = 4 }: { values: string[]; limit?: number }) {
  if (!values.length) return <span className="muted">—</span>;
  return (
    <div className="chip-list">
      {values.slice(0, limit).map((value) => (
        <span className="chip" key={value}>
          {value}
        </span>
      ))}
      {values.length > limit && <span className="chip">+{values.length - limit}</span>}
    </div>
  );
}

export function DetailGrid({ items }: { items: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="detail-grid">
      {items.map((item) => (
        <div key={item.label}>
          <dt>{item.label}</dt>
          <dd>{item.value || "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function KebabButton({ label = "More actions" }: { label?: string }) {
  return (
    <button className="icon-button" aria-label={label}>
      <MoreHorizontal size={18} />
    </button>
  );
}
