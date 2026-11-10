export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const r = await fetch(path, {
    method: opts.method ?? (opts.body ? "POST" : "GET"),
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : r.statusText);
  return data as T;
}

export const money = (cents?: number | null) => (cents == null ? "—" : `$${(cents / 100).toFixed(2)}`);

export function fmtTime(iso?: string | null, tz = "America/Los_Angeles") {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", { timeZone: tz, hour: "numeric", minute: "2-digit", month: "short", day: "numeric" });
}

export function dur(a?: string | null, b?: string | null) {
  if (!a || !b) return "—";
  const m = Math.round((new Date(b).getTime() - new Date(a).getTime()) / 60000);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

export const STATE_LABEL: Record<string, string> = {
  active: "Parked", charged: "Charged", review: "Held for review", disputed: "Disputed", refunded: "Refunded",
  pending_confirm: "Confirm exit", payment_failed: "Payment failed", void: "Void",
};
