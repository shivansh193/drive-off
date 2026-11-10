import { useEffect, useRef } from "react";
import { Scheduler, StringHelper } from "@bryntum/scheduler";
import "@bryntum/scheduler/scheduler.css";
import "@bryntum/scheduler/svalbard-dark.css";
import "@bryntum/scheduler/fontawesome/css/fontawesome.css";
import "@bryntum/scheduler/fontawesome/css/solid.css";
import { money } from "../api";

const COLOR: Record<string, string> = {
  active: "#f5c518", charged: "#3ccf7e", review: "#ff9f2e", payment_failed: "#ff9f2e", disputed: "#ff5a4e",
  refunded: "#5aa9ff", pending_confirm: "#b48cff", void: "#6b6d74",
};

type Props = { sessions: any[]; anon: any[]; now: string };

/** Live session timeline: one lane per vehicle, bars grow with the (demo) clock, coloured by state. */
export default function Timeline({ sessions, anon, now }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const sched = useRef<Scheduler | null>(null);

  useEffect(() => {
    if (!host.current) return;
    sched.current = new Scheduler({
      appendTo: host.current,
      readOnly: true,
      rowHeight: 44,
      barMargin: 7,
      viewPreset: "hourAndDay",
      startDate: new Date(Date.now() - 2 * 3600e3),
      endDate: new Date(Date.now() + 4 * 3600e3),
      columns: [{ text: "Vehicle", field: "name", width: 190, htmlEncode: false,
        renderer: ({ record }: any) => `<b>${StringHelper.encodeHtml(record.name)}</b><div style="font:11px 'Overpass Mono';opacity:.6">${StringHelper.encodeHtml(record.sub)}</div>` }],
      features: { timeRanges: { showHeaderElements: true }, eventTooltip: {
        template: ({ eventRecord }: any) =>
          `<b>${StringHelper.encodeHtml(eventRecord.name)}</b><br>${StringHelper.encodeHtml(eventRecord.getData("detail") ?? "")}` } },
      eventRenderer: ({ eventRecord, renderData }: any) => {
        renderData.eventColor = COLOR[eventRecord.getData("state")] ?? "#888";
        renderData.eventStyle = "filled";
        if (eventRecord.getData("growing")) renderData.cls.add("growing");
        return StringHelper.encodeHtml(eventRecord.name);
      },
    } as any);
    return () => { sched.current?.destroy(); sched.current = null; };
  }, []);

  useEffect(() => {
    const s = sched.current;
    if (!s) return;
    const nowD = new Date(now);
    const resources: any[] = [];
    const events: any[] = [];
    for (const x of sessions) {
      const rid = `r-${x.plate}-${x.id}`;
      resources.push({ id: rid, name: x.display_name ?? x.plate, sub: x.plate });
      const end = x.exit_at ? new Date(x.exit_at) : nowD;
      const label = x.state === "active" ? "Parked" : `${x.state.replace("_", " ")} ${x.amount_cents != null ? money(x.amount_cents) : ""}`;
      events.push({
        id: x.id, resourceId: rid, name: label, startDate: new Date(x.entry_at),
        endDate: new Date(Math.max(end.getTime(), new Date(x.entry_at).getTime() + 5 * 60e3)),
        state: x.state, growing: !x.exit_at,
        detail: `${x.plate} · ${(x.flags ?? []).join(", ") || "no flags"}${x.capture_id ? " · PayPal " + x.capture_id : ""}`,
      });
    }
    const openAnon = anon.filter((a) => !a.exit_at).length;
    if (anon.length) {
      resources.push({ id: "anon", name: `Not enrolled (${openAnon} parked)`, sub: "hashed · no images kept" });
      anon.forEach((a, i) => events.push({
        id: `anon-${i}`, resourceId: "anon", name: "visitor", startDate: new Date(a.entry_at),
        endDate: a.exit_at ? new Date(a.exit_at) : nowD, state: "void", detail: "Occupancy only. Plate hashed, image dropped.",
      }));
    }
    s.resourceStore.data = resources;
    s.eventStore.data = events;
    const starts = sessions.map((x) => new Date(x.entry_at).getTime()).concat(anon.map((a) => new Date(a.entry_at).getTime()));
    const start = new Date(Math.min(nowD.getTime() - 3600e3, ...starts) - 30 * 60e3);
    const end = new Date(nowD.getTime() + 2 * 3600e3);
    if (s.startDate.getTime() !== start.getTime() || s.endDate.getTime() !== end.getTime()) s.setTimeSpan(start, end);
    (s as any).timeRanges = [{ id: "now", name: "Now (demo clock)", startDate: nowD, cls: "now-line" }];
  }, [sessions, anon, now]);

  return <div className="timeline" ref={host} />;
}
