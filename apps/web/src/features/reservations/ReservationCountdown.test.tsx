import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReservationCountdown, reservationCountdownText } from "./ReservationCountdown";

describe("ReservationCountdown", () => {
  afterEach(() => vi.useRealTimers());

  it("warns inside ten minutes using the server-reported timestamp", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-23T12:00:00Z"));
    render(<ReservationCountdown endsAt="2026-08-23T12:09:30Z" />);

    expect(screen.getByText("Ends in 9m 30s")).toBeVisible();
    expect(screen.getByText("Save work or extend the lease soon.")).toBeVisible();
  });

  it("does not declare expiry before the server confirms it", () => {
    expect(
      reservationCountdownText("2026-08-23T12:00:00Z", new Date("2026-08-23T12:00:01Z").getTime()),
    ).toBe("Expiry awaiting server confirmation");
  });
});
