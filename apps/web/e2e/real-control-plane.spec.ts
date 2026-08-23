import { expect, test, type Page } from "@playwright/test";

const identities = {
  owner: { username: "michael", password: "Phase7-browser-demo!" },
  operator: { username: "operator", password: "Operator-browser-demo!" },
  viewer: { username: "viewer", password: "Viewer-browser-demo!" },
  labAdmin: { username: "lab-admin", password: "LabAdmin-browser-demo!" },
};

async function signIn(
  page: Page,
  identity: (typeof identities)[keyof typeof identities],
  to: string,
) {
  await page.goto(`/login?returnTo=${encodeURIComponent(to)}`);
  await page.getByLabel("Organisation").fill("simlab-demo");
  await page.getByLabel("Username").fill(identity.username);
  await page.getByLabel("Password").fill(identity.password);
  await page.getByRole("button", { name: /Sign in/ }).click();
  await expect(page).toHaveURL(new RegExp(`${to.replaceAll("/", "\\/")}$`));
}

async function signOut(page: Page) {
  await page.locator(".account-button").click();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
}

async function createUser(
  page: Page,
  user: {
    username: string;
    displayName: string;
    password: string;
    organisationRole: "MEMBER" | "VIEWER";
  },
) {
  await page.getByRole("button", { name: "Create user" }).click();
  const dialog = page.getByRole("dialog", { name: "Create user" });
  await dialog.getByLabel("Username").fill(user.username);
  await dialog.getByLabel("Display name").fill(user.displayName);
  await dialog.getByLabel("Organisation role").selectOption(user.organisationRole);
  await dialog.getByLabel("Initial password").fill(user.password);
  await dialog.getByRole("button", { name: "Create user" }).click();
  await expect(page.getByText("User created", { exact: true }).last()).toBeVisible();
}

test("complete Phase 7 role and SimLab browser flow uses real processes", async ({ page }) => {
  await signIn(page, identities.owner, "/admin/users");
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
  const organisationId = await page.evaluate(async () => {
    const response = await fetch("/api/v1/auth/me", { credentials: "include" });
    const payload = (await response.json()) as { organisation: { id: string } };
    return payload.organisation.id;
  });

  await createUser(page, {
    username: identities.operator.username,
    displayName: "Olivia Operator",
    password: identities.operator.password,
    organisationRole: "MEMBER",
  });
  await createUser(page, {
    username: identities.viewer.username,
    displayName: "Victor Viewer",
    password: identities.viewer.password,
    organisationRole: "VIEWER",
  });
  await createUser(page, {
    username: identities.labAdmin.username,
    displayName: "Lara Lab Admin",
    password: identities.labAdmin.password,
    organisationRole: "MEMBER",
  });

  await page.goto("/admin/teams");
  await page.getByRole("button", { name: "Create team" }).click();
  const createTeam = page.getByRole("dialog", { name: "Create team" });
  await createTeam.getByLabel("Name").fill("Phase 7 Operators");
  await createTeam.getByLabel("Slug").fill("phase7-operators");
  await createTeam.getByLabel("Description").fill("Operators created by the browser E2E flow");
  await createTeam.getByRole("button", { name: "Create team" }).click();
  await expect(page.getByText("Team created", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /Phase 7 Operators/ }).click();
  const teamDialog = page.getByRole("dialog", { name: "Phase 7 Operators" });
  await teamDialog.getByLabel("User").selectOption({ label: "Olivia Operator (@operator)" });
  await teamDialog.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText("Team member added", { exact: true })).toBeVisible();
  await teamDialog.getByRole("button", { name: "Done" }).click();

  await page.goto("/admin/roles");
  await expect(page.getByRole("heading", { name: "Roles & access" })).toBeVisible();
  const assignmentPanel = page
    .locator("section.panel")
    .filter({ has: page.getByRole("heading", { name: "Assign a role" }) });
  await assignmentPanel.getByLabel("Subject type").selectOption("TEAM");
  const subjectSelect = assignmentPanel.locator("select").nth(1);
  const teamOption = subjectSelect.locator("option", { hasText: "Phase 7 Operators" });
  await expect(teamOption).toHaveCount(1);
  const teamId = await teamOption.getAttribute("value");
  expect(teamId).toBeTruthy();
  await subjectSelect.selectOption(teamId!);
  await assignmentPanel.getByLabel("Role").selectOption("OPERATOR");
  await assignmentPanel.getByLabel("Resource type").selectOption("ORGANISATION");
  await assignmentPanel.getByLabel("Resource ID").fill(organisationId);
  await assignmentPanel.getByRole("button", { name: "Assign role" }).click();
  await expect(page.getByText("Role assigned", { exact: true }).last()).toBeVisible();

  await assignmentPanel.getByLabel("Subject type").selectOption("USER");
  await subjectSelect.selectOption({ label: "Lara Lab Admin" });
  await assignmentPanel.getByLabel("Role").selectOption("LAB_ADMIN");
  await assignmentPanel.getByLabel("Resource ID").fill(organisationId);
  await assignmentPanel.getByRole("button", { name: "Assign role" }).click();
  await expect(page.getByText("Role assigned", { exact: true }).last()).toBeVisible();

  await signOut(page);
  await signIn(page, identities.operator, "/benches");
  await expect(page.getByRole("heading", { name: "Benches" })).toBeVisible();
  const benchRows = page.locator("tbody tr");
  await expect(benchRows).toHaveCount(2);
  const benchRow = benchRows.first();
  await expect(benchRow).toContainText(/browser-(alpha|beta)/);
  const benchId = (await benchRow.locator("code").first().textContent())?.trim();
  expect(benchId).toBeTruthy();
  await benchRow.getByRole("link").first().click();

  await page.getByRole("button", { name: "Reserve", exact: true }).click();
  const reservationDialog = page.getByRole("dialog", { name: /Reserve / });
  await reservationDialog.getByLabel("Duration").fill("5");
  await reservationDialog.getByLabel("Description").fill("Phase 7 real-browser cut-line");
  await reservationDialog.getByRole("button", { name: "Reserve bench" }).click();
  await expect(page.getByText("Bench reserved", { exact: true })).toBeVisible();
  await expect(page.getByText("Current owner", { exact: true })).toBeVisible();

  await page.goto(`/workflows?bench=${encodeURIComponent(benchId!)}`);
  const workflowCard = page.locator("article.workflow-card", { hasText: "phase7-cutline" });
  await workflowCard.getByRole("button", { name: /Run workflow/ }).click();
  const workflowDialog = page.getByRole("dialog", { name: "Run phase7-cutline" });
  await expect(workflowDialog.getByLabel("Specific bench")).toBeChecked();
  await expect(workflowDialog.getByLabel("Use active reservation")).toBeChecked();
  await workflowDialog.getByRole("button", { name: "Run workflow", exact: true }).click();

  await expect(page).toHaveURL(/\/workflow-runs\//);
  await expect(page.getByRole("heading", { name: "phase7-cutline" })).toBeVisible();
  await expect(page.getByText("Succeeded", { exact: true }).first()).toBeVisible({
    timeout: 60_000,
  });

  await page.goto("/artifacts");
  const serialArtifact = page
    .locator("tbody tr")
    .filter({ hasText: /serial/i })
    .first();
  await expect(serialArtifact).toBeVisible({ timeout: 45_000 });
  await serialArtifact.getByRole("button", { name: "Preview" }).click();
  const preview = page.getByRole("dialog", { name: /Preview/ });
  await expect(preview.locator("pre.artifact-preview")).toContainText("READY");
  await preview.getByRole("button", { name: "Done" }).click();

  await page.goto(`/benches/${encodeURIComponent(benchId!)}`);
  await page.getByRole("button", { name: "Release", exact: true }).first().click();
  const releaseDialog = page.getByRole("dialog", { name: /Release / });
  await releaseDialog.getByRole("button", { name: "Release reservation" }).click();
  await expect(page.getByText("Reservation released", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reserve", exact: true })).toBeVisible();

  await signOut(page);
  await signIn(page, identities.viewer, "/benches");
  const viewerBenchRow = page.locator("tbody tr").first();
  await viewerBenchRow.getByRole("link").first().click();
  await expect(page.getByRole("button", { name: "Reserve", exact: true })).toBeDisabled();
  await expect(page.getByRole("link", { name: "Run workflow" })).toHaveCount(0);
  const denial = await page.evaluate(async () => {
    const csrf = document.cookie
      .split(";")
      .map((part) => part.trim().split("="))
      .find(([name]) => name === "lab_csrf")?.[1];
    const response = await fetch("/api/v1/teams", {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}),
      },
      body: JSON.stringify({
        slug: `phase7-viewer-denial-${crypto.randomUUID()}`,
        name: "Forbidden Viewer Team",
      }),
    });
    const payload = (await response.json()) as {
      error?: { code?: string };
      detail?: { code?: string };
    };
    return { status: response.status, code: payload.error?.code ?? payload.detail?.code };
  });
  expect(denial).toEqual({ status: 403, code: "PERMISSION_DENIED" });
  await page.goto("/admin/users");
  await expect(
    page.getByRole("heading", { name: "This area isn’t available to your role" }),
  ).toBeVisible();

  await signOut(page);
  await signIn(page, identities.labAdmin, "/agents");
  const agentRow = page.locator("tbody tr").first();
  await expect(page.locator("tbody tr")).toHaveCount(2);
  await agentRow.getByRole("link").first().click();
  await page.getByRole("button", { name: "Drain Agent" }).click();
  const drainDialog = page.getByRole("dialog", { name: /Drain Agent/ });
  await drainDialog.getByRole("button", { name: "Drain Agent" }).click();
  await expect(page.getByText("Drain requested", { exact: true })).toBeVisible();

  await signOut(page);
  await signIn(page, identities.owner, "/audit");
  await page.getByPlaceholder("Filter by actor display name…").fill("Victor Viewer");
  await page.getByLabel("Outcome").selectOption("DENIED");
  await expect(page.locator("tbody tr").first()).toContainText("Victor Viewer", {
    timeout: 30_000,
  });
  await expect(page.locator("tbody tr").first()).toContainText("Denied");
});
