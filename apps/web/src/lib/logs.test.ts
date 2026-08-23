import { describe, expect, it } from "vitest";
import { boundedLogLines } from "./logs";
import { formatBytes, shortId, titleCase } from "./format";
import { isActiveStatus, statusTone } from "./status";

describe("boundedLogLines", () => {
  it("retains only the newest bounded output", () => {
    const lines = Array.from({ length: 5_500 }, (_, index) => `line-${index}`);
    const bounded = boundedLogLines(lines, 5_000);
    expect(bounded).toHaveLength(5_000);
    expect(bounded[0]).toBe("line-500");
    expect(bounded.at(-1)).toBe("line-5499");
  });

  it("rejects invalid limits", () => {
    expect(() => boundedLogLines(["line"], 0)).toThrow(RangeError);
  });
});

describe("presentation formatting", () => {
  it("preserves unknown distributed state instead of calling it failed", () => {
    expect(isActiveStatus("UNKNOWN")).toBe(true);
    expect(statusTone("UNKNOWN")).not.toBe("negative");
  });

  it("formats identifiers, statuses, and sizes", () => {
    expect(shortId("12345678-abcd-ef00-1234-56789abcdef0")).toBe("12345678…def0");
    expect(titleCase("RECONCILING")).toBe("Reconciling");
    expect(formatBytes(1_024)).toContain("KB");
  });
});
