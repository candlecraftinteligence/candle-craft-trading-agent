export type StateTone = "live" | "progress" | "risk" | "resolved" | "neutral";

export function toneForState(state: string): StateTone {
  switch (state) {
    case "ACTIVE":
    case "WATCH":
    case "STALKING":
    case "TRIGGERED":
    case "CONFIRMED":
      return "live";
    case "TP1":
    case "TP2":
    case "TP_HIT":
      return "progress";
    case "SL_HIT":
    case "INVALIDATED":
      return "risk";
    case "EXPIRED":
    case "CLOSED":
      return "resolved";
    default:
      return "neutral";
  }
}
