import { describe, expect, it } from "vitest";
import {
  reservationActionAllowed,
  reservationAdministrator,
  reservationOwnedByCaller,
} from "./permissions";

describe("reservation permissions", () => {
  it("reads wrapper and summary permission objects without falling back to global roles", () => {
    const wrapper = {
      reservation: { id: "reservation-1" },
      permissions: {
        owned_by_caller: false,
        release: true,
        extend: false,
        cancel: false,
        administrator: true,
      },
    };
    const summary = {
      id: "reservation-2",
      permissions: {
        owned_by_caller: true,
        release: true,
        extend: true,
        cancel: false,
        administrator: false,
      },
    };

    expect(reservationActionAllowed(wrapper, "release")).toBe(true);
    expect(reservationActionAllowed(wrapper, "extend")).toBe(false);
    expect(reservationAdministrator(wrapper)).toBe(true);
    expect(reservationOwnedByCaller(wrapper)).toBe(false);
    expect(reservationActionAllowed(summary, "extend")).toBe(true);
    expect(reservationOwnedByCaller(summary)).toBe(true);
  });
});
