import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api/client";
import { ProtectedRoute } from "../pages/SystemPages";
import { AppShell } from "./AppShell";
import { AuthProvider } from "./AuthProvider";
import { LiveProvider } from "./LiveProvider";
import { ToastProvider } from "./ToastProvider";

const sessionPayload = {
  principal: { id: "user-1", username: "alice", display_name: "Alice Operator" },
  organisation: { id: "org-1", slug: "simlab", name: "SimLab" },
  permissions: ["benches:read"],
  roles: ["OPERATOR"],
};

function ExpiryTrigger() {
  return (
    <button type="button" onClick={() => void apiFetch("/api/v1/protected").catch(() => undefined)}>
      Expire session
    </button>
  );
}

function TestApplication() {
  return (
    <Routes>
      <Route path="/login" element={<h1>Login screen</h1>} />
      <Route element={<ProtectedRoute />}>
        <Route element={<AppShell />}>
          <Route
            index
            element={
              <>
                <Outlet />
                <ExpiryTrigger />
              </>
            }
          />
        </Route>
      </Route>
    </Routes>
  );
}

function renderApplication(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Number.POSITIVE_INFINITY } },
  });
  const result = render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <AuthProvider>
          <LiveProvider>
            <MemoryRouter initialEntries={["/"]}>
              <TestApplication />
            </MemoryRouter>
          </LiveProvider>
        </AuthProvider>
      </ToastProvider>
    </QueryClientProvider>,
  );
  return { ...result, client };
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("browser session lifecycle", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("clears all private query data immediately after logout", async () => {
    let signedIn = true;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = input instanceof Request ? input.url : String(input);
      if (path.endsWith("/auth/config"))
        return jsonResponse({ local_enabled: true, web: { live_updates: {} } });
      if (path.endsWith("/auth/me"))
        return signedIn ? jsonResponse(sessionPayload) : jsonResponse({ error: {} }, 401);
      if (path.endsWith("/auth/logout")) {
        signedIn = false;
        return new Response(null, { status: 204 });
      }
      return jsonResponse({ items: [] });
    });
    const { client } = renderApplication(fetchMock);
    client.setQueryData(["benches"], { items: [{ id: "private-bench" }] });
    client.setQueryData(["audit"], { items: [{ actor: "private-user" }] });

    await userEvent.click(await screen.findByRole("button", { name: /Alice Operator/ }));
    await waitFor(() => expect(client.getQueryData(["auth", "config"])).toBeDefined());
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await expect(screen.findByRole("heading", { name: "Login screen" })).resolves.toBeVisible();
    expect(client.getQueryData(["benches"])).toBeUndefined();
    expect(client.getQueryData(["audit"])).toBeUndefined();
    expect(client.getQueryData(["auth", "config"])).toBeDefined();
  });

  it("returns to login after a protected request and refresh both reject an expired session", async () => {
    let currentSessionRequests = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = input instanceof Request ? input.url : String(input);
      if (path.endsWith("/auth/config"))
        return jsonResponse({ local_enabled: true, web: { live_updates: {} } });
      if (path.endsWith("/auth/me")) {
        currentSessionRequests += 1;
        return currentSessionRequests === 1
          ? jsonResponse(sessionPayload)
          : jsonResponse({ error: {} }, 401);
      }
      if (path.endsWith("/auth/refresh")) return jsonResponse({ error: {} }, 401);
      if (path.endsWith("/protected")) return jsonResponse({ error: {} }, 401);
      return jsonResponse({ items: [] });
    });
    renderApplication(fetchMock);

    await userEvent.click(await screen.findByRole("button", { name: "Expire session" }));

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Login screen" })).toBeVisible(),
    );
    expect(
      fetchMock.mock.calls.some(([input]) =>
        (input instanceof Request ? input.url : String(input)).endsWith("/auth/refresh"),
      ),
    ).toBe(true);
  });
});
