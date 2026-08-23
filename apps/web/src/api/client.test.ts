import { afterEach, describe, expect, expectTypeOf, it, vi } from "vitest";
import { ApiError, SESSION_EXPIRED_EVENT, apiFetch, generatedApi } from "./client";

describe("apiFetch", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.cookie = "lab_csrf=; Max-Age=0; Path=/";
  });

  it("sends cookie credentials and the readable CSRF token for mutations", async () => {
    document.cookie = "lab_csrf=csrf-test-token; Path=/";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/api/v1/reservations", {
      method: "POST",
      body: JSON.stringify({ bench_id: "simlab/bench-01" }),
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.credentials).toBe("include");
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf-test-token");
  });

  it.each([
    ["BENCH_ALREADY_RESERVED", 409, "This bench is currently reserved by another operator."],
    ["PERMISSION_DENIED", 403, "You do not have permission to perform this action."],
  ])("unwraps the %s error envelope and retains its request ID", async (code, status, message) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code,
              message: "internal text",
              details: { bench_id: "bench-1" },
              request_id: "req-42",
            },
          }),
          { status, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const error = await apiFetch("/api/v1/operations/42").catch((value: unknown) => value);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      code,
      requestId: "req-42",
      message,
      details: { bench_id: "bench-1" },
    });
  });

  it("uses one refresh request for concurrent 401 responses and retries each request once", async () => {
    let refreshed = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/auth/refresh")) {
        refreshed = true;
        return new Response(JSON.stringify({ refreshed: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify(refreshed ? { ok: true } : { error: {} }), {
        status: refreshed ? 200 : 401,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all([apiFetch("/api/v1/benches"), apiFetch("/api/v1/agents")]);

    expect(
      fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/auth/refresh")),
    ).toHaveLength(1);
  });

  it("rebuilds a retried mutation with the CSRF token rotated by refresh", async () => {
    document.cookie = "lab_csrf=old-token; Path=/";
    let mutationAttempts = 0;
    const retryHeaders: Headers[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/auth/refresh")) {
        document.cookie = "lab_csrf=new-token; Path=/";
        return new Response(JSON.stringify({ refreshed: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      mutationAttempts += 1;
      retryHeaders.push(new Headers(init?.headers));
      return new Response(JSON.stringify(mutationAttempts === 1 ? { error: {} } : { ok: true }), {
        status: mutationAttempts === 1 ? 401 : 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/api/v1/reservations", {
      method: "POST",
      body: JSON.stringify({ bench_id: "simlab/bench-01" }),
    });

    expect(retryHeaders.map((headers) => headers.get("X-CSRF-Token"))).toEqual([
      "old-token",
      "new-token",
    ]);
  });

  it("signals terminal session expiry when refresh cannot extend a 401 response", async () => {
    const expired = vi.fn();
    window.addEventListener(SESSION_EXPIRED_EVENT, expired);
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL) =>
          new Response(JSON.stringify({ error: {} }), {
            status: String(input).endsWith("/auth/refresh") ? 403 : 401,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );

    await expect(apiFetch("/api/v1/benches")).rejects.toMatchObject({ status: 401 });

    expect(expired).toHaveBeenCalledOnce();
    window.removeEventListener(SESSION_EXPIRED_EVENT, expired);
  });

  it("serializes generated path parameters and typed workflow bodies through the shared transport", async () => {
    document.cookie = "lab_csrf=generated-csrf; Path=/";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ operation_id: "operation-42" }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const body = {
      idempotency_key: "web-workflow:test",
      version: 2,
      bench_id: null,
      inputs: { mode: "safe" },
      command_timeout_seconds: 600,
    };
    await generatedApi.runWorkflow("smoke test", body);

    const [request] = fetchMock.mock.calls[0] as [Request];
    expect(new URL(request.url).pathname).toBe("/api/v1/workflows/smoke%20test/runs");
    expect(request.method).toBe("POST");
    expect(request.credentials).toBe("include");
    expect(request.headers.get("X-CSRF-Token")).toBe("generated-csrf");
    await expect(request.clone().json()).resolves.toEqual(body);
    expectTypeOf<Parameters<typeof generatedApi.runWorkflow>[1]>().toMatchTypeOf<{
      idempotency_key: string;
      command_timeout_seconds: number;
    }>();
  });

  it("serializes generated audit query parameters and preserves API error mapping", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            error: {
              code: "PERMISSION_DENIED",
              message: "internal text",
              request_id: "req-generated",
            },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    await generatedApi.listAuditEvents({
      actor: "operator",
      outcome: "DENIED",
      cursor: "cursor-2",
      limit: 100,
    });
    const [auditRequest] = fetchMock.mock.calls[0] as [Request];
    const auditUrl = new URL(auditRequest.url);
    expect(auditUrl.pathname).toBe("/api/v1/audit-events");
    expect(auditUrl.searchParams.get("actor")).toBe("operator");
    expect(auditUrl.searchParams.get("outcome")).toBe("DENIED");
    expect(auditUrl.searchParams.get("cursor")).toBe("cursor-2");
    expect(auditUrl.searchParams.get("limit")).toBe("100");

    await expect(
      generatedApi.createTeam({ slug: "qa", name: "QA", description: null }),
    ).rejects.toMatchObject({
      status: 403,
      code: "PERMISSION_DENIED",
      requestId: "req-generated",
      message: "You do not have permission to perform this action.",
    });
  });
});
