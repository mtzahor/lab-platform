import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "./AuthProvider";

function PermissionProbe() {
  const auth = useAuth();
  if (auth.loading) return <span>Loading</span>;
  return <span>{auth.can("benches:flash") ? "Can flash" : "Read only"}</span>;
}

function renderProvider(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <PermissionProbe />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("AuthProvider", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses server-provided effective permissions", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = input instanceof Request ? input.url : String(input);
      const payload = path.endsWith("/auth/config")
        ? {
            local_enabled: true,
            oidc_enabled: false,
            web: { live_updates: { sse_enabled: true, polling_fallback_seconds: 7 } },
          }
        : {
            principal: { id: "user-1", display_name: "Alice" },
            organisation: { id: "org-1", name: "SimLab" },
            permissions: ["benches:read", "benches:flash"],
            roles: ["OPERATOR"],
          };
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    renderProvider(fetchMock);
    await waitFor(() => expect(screen.getByText("Can flash")).toBeVisible());
  });

  it("does not reconstruct missing RBAC permissions in the browser", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = input instanceof Request ? input.url : String(input);
      const payload = path.endsWith("/auth/config")
        ? { local_enabled: true, oidc_enabled: false, web: { live_updates: {} } }
        : {
            principal: { id: "user-2", display_name: "Bob" },
            organisation: { id: "org-1", name: "SimLab" },
            permissions: ["benches:read"],
            roles: ["VIEWER"],
          };
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    renderProvider(fetchMock);
    await waitFor(() => expect(screen.getByText("Read only")).toBeVisible());
  });
});
