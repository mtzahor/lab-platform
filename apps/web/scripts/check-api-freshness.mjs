import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const temp = mkdtempSync(path.join(tmpdir(), "lab-platform-openapi-"));
const generated = path.join(temp, "schema.ts");

try {
  execFileSync(
    process.execPath,
    [
      path.join(root, "node_modules/openapi-typescript/bin/cli.js"),
      "../../docs/control-plane-openapi.json",
      "-o",
      generated,
    ],
    { cwd: root, stdio: "pipe" },
  );
  const current = readFileSync(path.join(root, "src/api/schema.ts"), "utf8");
  const expected = readFileSync(generated, "utf8");
  if (current !== expected) {
    console.error("Generated API client is stale. Run `npm run api:generate` in apps/web.");
    process.exitCode = 1;
  }
} finally {
  rmSync(temp, { recursive: true, force: true });
}
