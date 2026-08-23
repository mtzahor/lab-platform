import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { Download, Eye, FileArchive, RefreshCw, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  generatedApi,
  apiBase,
  errorMessage,
  nested,
  records,
  stringValue,
  type ApiRecord,
} from "../api/client";
import { useAuth } from "../app/AuthProvider";
import { useToast } from "../app/ToastProvider";
import {
  Button,
  ConfirmDialog,
  DataTable,
  DetailGrid,
  Dialog,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  SearchField,
} from "../components/ui";
import { useApiList, useApiMutation } from "../hooks/useApi";
import { formatBytes, formatDate, formatRelative, shortId, titleCase } from "../lib/format";

const MAX_TEXT_PREVIEW_BYTES = 512 * 1024;

function canPreview(artifact: ApiRecord): boolean {
  if (Number(stringValue(artifact, "size_bytes") ?? 0) > MAX_TEXT_PREVIEW_BYTES) return false;
  const content = (stringValue(artifact, "content_type") ?? "").toLowerCase();
  const name = (stringValue(artifact, "name") ?? "").toLowerCase();
  const type = (stringValue(artifact, "artifact_type") ?? "").toLowerCase();
  return (
    content.startsWith("text/") ||
    content.includes("json") ||
    content.includes("xml") ||
    /\.(log|txt|json|xml|junit)$/.test(name) ||
    ["serial_log", "junit", "workflow_summary", "json"].some((value) => type.includes(value))
  );
}

export function ArtifactsPage() {
  const auth = useAuth();
  const { notify } = useToast();
  const [params] = useSearchParams();
  const query = useApiList("artifacts", "/api/v1/artifacts?limit=1000", true, () =>
    generatedApi.listArtifacts({ limit: 1000 }),
  );
  const [search, setSearch] = useState("");
  const [type, setType] = useState("");
  const [owner, setOwner] = useState("");
  const [selected, setSelected] = useState<ApiRecord>();
  const [preview, setPreview] = useState<ApiRecord>();
  const [previewText, setPreviewText] = useState<string>();
  const [previewLoading, setPreviewLoading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ApiRecord>();
  const items = records(query.data);
  const bench = params.get("bench") ?? "";
  const types = [
    ...new Set(items.map((item) => stringValue(item, "artifact_type")).filter(Boolean) as string[]),
  ].sort();
  const owners = [
    ...new Set(items.map((item) => stringValue(item, "owner_type")).filter(Boolean) as string[]),
  ].sort();
  const filtered = items.filter(
    (item) =>
      (!search ||
        `${stringValue(item, "name")} ${stringValue(item, "id")} ${stringValue(item, "sha256")} ${stringValue(item, "owner_id")}`
          .toLowerCase()
          .includes(search.toLowerCase())) &&
      (!type || stringValue(item, "artifact_type") === type) &&
      (!owner || stringValue(item, "owner_type") === owner) &&
      (!bench ||
        stringValue(item, "bench_id") === bench ||
        stringValue(nested(item, "metadata"), "bench_id") === bench),
  );
  const deletion = useApiMutation(
    (artifact: ApiRecord) => generatedApi.deleteArtifact(stringValue(artifact, "id") ?? ""),
    [["artifacts"]],
  );
  async function remove() {
    if (!deleteTarget) return;
    try {
      await deletion.mutateAsync(deleteTarget);
      notify({
        title: "Artifact deleted",
        message: `${stringValue(deleteTarget, "name")} was removed.`,
        tone: "success",
      });
      setDeleteTarget(undefined);
    } catch (error) {
      notify({ title: "Deletion failed", message: errorMessage(error), tone: "error" });
    }
  }
  async function openPreview(artifact: ApiRecord) {
    setPreview(artifact);
    setPreviewText(undefined);
    const size = Number(stringValue(artifact, "size_bytes") ?? 0);
    if (size > MAX_TEXT_PREVIEW_BYTES) {
      setPreviewText(
        `Preview is limited to ${formatBytes(MAX_TEXT_PREVIEW_BYTES)}. Download the original artifact instead.`,
      );
      return;
    }
    setPreviewLoading(true);
    try {
      const content = await generatedApi.getArtifactContent(stringValue(artifact, "id") ?? "");
      if (content instanceof Blob) setPreviewText(await content.text());
      else setPreviewText(JSON.stringify(content, null, 2));
    } catch (error) {
      setPreviewText(errorMessage(error));
    } finally {
      setPreviewLoading(false);
    }
  }
  const columns = useMemo<ColumnDef<ApiRecord, any>[]>(() => {
    const column = createColumnHelper<ApiRecord>();
    return [
      column.accessor((row) => stringValue(row, "name") ?? "", {
        id: "name",
        header: "Artifact",
        cell: ({ row, getValue }) => (
          <div className="primary-cell">
            <button className="link-button" onClick={() => setSelected(row.original)}>
              {getValue()}
            </button>
            <code>{shortId(stringValue(row.original, "id"))}</code>
          </div>
        ),
      }),
      column.accessor((row) => stringValue(row, "artifact_type") ?? "", {
        id: "type",
        header: "Type",
        cell: ({ getValue }) => <span className="type-badge">{titleCase(getValue())}</span>,
      }),
      column.accessor((row) => stringValue(row, "owner_type") ?? "", {
        id: "owner",
        header: "Owner",
        cell: ({ row, getValue }) => (
          <div className="stacked-cell">
            <span>{titleCase(getValue())}</span>
            <code>{shortId(stringValue(row.original, "owner_id"))}</code>
          </div>
        ),
      }),
      column.accessor(
        (row) =>
          stringValue(row, "bench_id") ?? stringValue(nested(row, "metadata"), "bench_id") ?? "",
        {
          id: "bench",
          header: "Bench",
          cell: ({ getValue }) =>
            getValue() ? (
              <Link to={`/benches/${encodeURIComponent(getValue())}`}>{getValue()}</Link>
            ) : (
              "—"
            ),
        },
      ),
      column.accessor((row) => Number(stringValue(row, "size_bytes") ?? 0), {
        id: "size",
        header: "Size",
        cell: ({ getValue }) => formatBytes(getValue()),
      }),
      column.accessor((row) => stringValue(row, "sha256") ?? "", {
        id: "checksum",
        header: "Checksum",
        cell: ({ getValue }) => (
          <code title={getValue()}>{getValue() ? `${getValue().slice(0, 10)}…` : "—"}</code>
        ),
      }),
      column.accessor((row) => stringValue(row, "created_at") ?? "", {
        id: "created",
        header: "Created",
        cell: ({ getValue }) => formatRelative(getValue()),
      }),
      column.accessor((row) => stringValue(row, "expires_at") ?? "", {
        id: "expiry",
        header: "Expiry",
        cell: ({ getValue }) => (getValue() ? formatRelative(getValue()) : "Retained"),
      }),
      column.display({
        id: "actions",
        header: "",
        cell: ({ row }) => (
          <div className="table-actions">
            {canPreview(row.original) && (
              <Button
                variant="ghost"
                icon={Eye}
                onClick={(event) => {
                  event.stopPropagation();
                  void openPreview(row.original);
                }}
              >
                Preview
              </Button>
            )}
            <a
              className="button button-ghost"
              href={`${apiBase}/api/v1/artifacts/${stringValue(row.original, "id")}/content`}
              download
              onClick={(event) => event.stopPropagation()}
            >
              <Download size={15} /> Download
            </a>
            {auth.can("artifacts:delete") && (
              <Button
                variant="ghost"
                icon={Trash2}
                onClick={(event) => {
                  event.stopPropagation();
                  setDeleteTarget(row.original);
                }}
              >
                Delete
              </Button>
            )}
          </div>
        ),
      }),
    ];
  }, [auth]);
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Results"
        title="Artifacts"
        description="Logs, firmware, reports and workflow output accessed through the control plane."
        actions={
          <Button variant="secondary" icon={RefreshCw} onClick={() => void query.refetch()}>
            Refresh
          </Button>
        }
      />
      <div className="toolbar">
        <SearchField
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search name, checksum, owner or ID…"
        />
        <label className="compact-select">
          <span className="sr-only">Type</span>
          <select value={type} onChange={(event) => setType(event.target.value)}>
            <option value="">All types</option>
            {types.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="compact-select">
          <span className="sr-only">Owner type</span>
          <select value={owner} onChange={(event) => setOwner(event.target.value)}>
            <option value="">All owners</option>
            {owners.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        {bench && <span className="active-filter">Bench: {bench}</span>}
      </div>
      {query.isLoading ? (
        <LoadingState label="Loading artifacts" />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title="No artifacts"
          description="Serial logs, JUnit reports and workflow output will appear here."
          icon={FileArchive}
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="No artifacts match"
          description="Adjust the search or filters."
          icon={FileArchive}
        />
      ) : (
        <DataTable data={filtered} columns={columns} pageSize={50} />
      )}
      <Dialog
        open={Boolean(selected)}
        onClose={() => setSelected(undefined)}
        title={stringValue(selected, "name") ?? "Artifact metadata"}
        description="Controlled metadata; storage paths and secrets are not exposed."
      >
        <DetailGrid
          items={[
            { label: "Artifact ID", value: <code>{stringValue(selected, "id")}</code> },
            { label: "Type", value: titleCase(stringValue(selected, "artifact_type")) },
            {
              label: "Owner",
              value: `${titleCase(stringValue(selected, "owner_type"))} · ${shortId(stringValue(selected, "owner_id"))}`,
            },
            { label: "Content type", value: stringValue(selected, "content_type") },
            { label: "Size", value: formatBytes(Number(stringValue(selected, "size_bytes"))) },
            { label: "Created", value: formatDate(stringValue(selected, "created_at")) },
            { label: "Expires", value: formatDate(stringValue(selected, "expires_at")) },
            {
              label: "SHA-256",
              value: <code className="wrap-code">{stringValue(selected, "sha256")}</code>,
            },
          ]}
        />
        {nested(selected, "metadata") && (
          <>
            <h3>Metadata</h3>
            <pre className="metadata-viewer">
              {JSON.stringify(nested(selected, "metadata"), null, 2)}
            </pre>
          </>
        )}
        <div className="dialog-actions">
          <a
            className="button button-primary"
            href={`${apiBase}/api/v1/artifacts/${stringValue(selected, "id")}/content`}
            download
          >
            <Download size={15} /> Download
          </a>
        </div>
      </Dialog>
      <Dialog
        open={Boolean(preview)}
        onClose={() => {
          setPreview(undefined);
          setPreviewText(undefined);
        }}
        title={`Preview · ${stringValue(preview, "name")}`}
        description={`Text-only preview, limited to ${formatBytes(MAX_TEXT_PREVIEW_BYTES)}. Binary artifacts are never rendered.`}
        size="wide"
      >
        {previewLoading ? (
          <LoadingState label="Loading preview" />
        ) : (
          <pre className="artifact-preview">{previewText}</pre>
        )}
        <div className="dialog-actions">
          <a
            className="button button-secondary"
            href={`${apiBase}/api/v1/artifacts/${stringValue(preview, "id")}/content`}
            download
          >
            <Download size={15} /> Download original
          </a>
          <Button onClick={() => setPreview(undefined)}>Done</Button>
        </div>
      </Dialog>
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title={`Delete ${stringValue(deleteTarget, "name")}?`}
        message={
          <>
            Permanently delete artifact <strong>{stringValue(deleteTarget, "name")}</strong>?
            Existing audit history remains, but the content may not be recoverable.
          </>
        }
        confirmLabel="Delete artifact"
        onConfirm={() => void remove()}
        onClose={() => setDeleteTarget(undefined)}
        busy={deletion.isPending}
      />
    </div>
  );
}
