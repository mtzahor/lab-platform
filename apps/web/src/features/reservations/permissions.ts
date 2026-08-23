import { asRecord, booleanValue, nested, type ApiRecord } from "../../api/client";

export type ReservationAction = "release" | "extend" | "cancel";

export function reservationPermissions(item: ApiRecord | undefined): ApiRecord | undefined {
  if (!item) return undefined;
  return nested(item, "permissions") ?? nested(asRecord(item.reservation), "permissions");
}

export function reservationActionAllowed(
  item: ApiRecord | undefined,
  action: ReservationAction,
): boolean {
  return booleanValue(reservationPermissions(item), action);
}

export function reservationOwnedByCaller(item: ApiRecord | undefined): boolean {
  return booleanValue(reservationPermissions(item), "owned_by_caller");
}

export function reservationAdministrator(item: ApiRecord | undefined): boolean {
  return booleanValue(reservationPermissions(item), "administrator");
}

export function reservationActionExplanation(action: ReservationAction): string {
  if (action === "extend") return "Only the reservation owner can extend this lease.";
  if (action === "cancel") {
    return "Only the reservation owner or a bench administrator can cancel this slot.";
  }
  return "Only the reservation owner or a bench administrator can release this reservation.";
}
