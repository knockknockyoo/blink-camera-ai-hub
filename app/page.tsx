"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

type EventKind = "person" | "vehicle" | "motion" | "noise";

type MonitorEvent = {
  id: number;
  camera: string;
  started_at: string;
  ended_at: string;
  kind: EventKind;
  score: number;
  anomaly: boolean;
  anomaly_reasons: string[];
  labels: Record<string, number>;
  clip_ids: number[];
  video_path: string | null;
};

type Status = {
  configured: boolean;
  scanning: boolean;
  interval_seconds: number;
  last_scan: string | null;
  last_error: string | null;
  counts: { total: number; people: number; animals: number; anomalies: number };
};

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8787";

const DEMO_EVENTS: MonitorEvent[] = [
  {
    id: -1,
    camera: "Outdoor",
    started_at: "2026-07-16T19:42:00-04:00",
    ended_at: "2026-07-16T19:44:00-04:00",
    kind: "person",
    score: 0.94,
    anomaly: true,
    anomaly_reasons: ["Repeated activity in a short period"],
    labels: { person: 1 },
    clip_ids: [1, 2, 3],
    video_path: null,
  },
  {
    id: -2,
    camera: "Outdoor",
    started_at: "2026-07-16T17:53:00-04:00",
    ended_at: "2026-07-16T17:53:00-04:00",
    kind: "vehicle",
    score: 0.89,
    anomaly: false,
    anomaly_reasons: [],
    labels: { car: 1 },
    clip_ids: [4],
    video_path: null,
  },
  {
    id: -3,
    camera: "Outdoor",
    started_at: "2026-07-16T15:00:00-04:00",
    ended_at: "2026-07-16T15:00:00-04:00",
    kind: "motion",
    score: 0,
    anomaly: false,
    anomaly_reasons: [],
    labels: {},
    clip_ids: [5],
    video_path: null,
  },
];

const LABELS: Record<string, string> = {
  person: "Person",
  vehicle: "Vehicle",
  motion: "Unclassified motion",
  noise: "Noise",
  bicycle: "Bicycle",
  car: "Car",
  motorcycle: "Motorcycle",
  bus: "Bus",
  truck: "Truck",
};

function kindLabel(kind: string) {
  return LABELS[kind] || kind;
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Seoul",
  }).format(new Date(value));
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    month: "long",
    day: "numeric",
    weekday: "short",
    timeZone: "Asia/Seoul",
  }).format(new Date(value));
}

function relativeTime(value: string | null) {
  if (!value) return "Never";
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60_000));
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes} min ago`;
  return `${Math.floor(minutes / 60)} hr ago`;
}

export default function Home() {
  const [events, setEvents] = useState<MonitorEvent[]>(DEMO_EVENTS);
  const [status, setStatus] = useState<Status | null>(null);
  const [selectedId, setSelectedId] = useState<number>(DEMO_EVENTS[0].id);
  const [filter, setFilter] = useState<"important" | "person" | "vehicle" | "anomaly" | "all">("important");
  const [backendOnline, setBackendOnline] = useState(false);
  const [scanPending, setScanPending] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [statusResponse, eventsResponse] = await Promise.all([
        fetch(`${API}/api/status`, { cache: "no-store" }),
        fetch(`${API}/api/events?limit=100`, { cache: "no-store" }),
      ]);
      if (!statusResponse.ok || !eventsResponse.ok) throw new Error("backend unavailable");
      const nextStatus: Status = await statusResponse.json();
      const payload: { events: MonitorEvent[] } = await eventsResponse.json();
      setStatus(nextStatus);
      setBackendOnline(true);
      if (payload.events.length) {
        setEvents(payload.events);
        setSelectedId((current) =>
          payload.events.some((event) => event.id === current) ? current : payload.events[0].id,
        );
      }
    } catch {
      setBackendOnline(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(refresh, 0);
    const timer = window.setInterval(refresh, 30_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [refresh]);

  const filtered = useMemo(
    () =>
      events.filter((event) => {
        if (filter === "all") return true;
        if (filter === "important") return event.kind === "person" || event.kind === "vehicle" || event.anomaly;
        if (filter === "anomaly") return event.anomaly;
        return event.kind === filter;
      }),
    [events, filter],
  );
  const selected = events.find((event) => event.id === selectedId) || filtered[0];
  const counts = status?.counts || {
    total: events.length,
    people: events.filter((event) => event.kind === "person").length,
    animals: events.filter((event) => event.kind === "animal").length,
    anomalies: events.filter((event) => event.anomaly).length,
  };

  async function scanNow() {
    if (!backendOnline || scanPending) return;
    setScanPending(true);
    try {
      await fetch(`${API}/api/scan`, { method: "POST" });
      await refresh();
    } finally {
      setScanPending(false);
    }
  }

  function playNext() {
    if (!selected) return;
    const index = filtered.findIndex((event) => event.id === selected.id);
    const next = filtered[index + 1];
    if (next) setSelectedId(next.id);
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true"><i /></span>
          <div>
            <strong>Blink Camera AI Hub</strong>
            <span>Local AI Camera Monitor</span>
          </div>
        </div>
        <div className="system-state">
          <span className={`pulse ${backendOnline ? "online" : "demo"}`} />
          <div>
            <strong>{backendOnline ? (status?.configured ? "Blink connected" : "Local mode") : "Demo preview"}</strong>
            <span>{backendOnline ? `Last checked ${relativeTime(status?.last_scan || null)}` : "Start the backend to load live data"}</span>
          </div>
          <button className="scan-button" onClick={scanNow} disabled={!backendOnline || scanPending}>
            {scanPending ? "Scanning…" : "Scan now"}
          </button>
        </div>
      </header>

      <section className="overview" aria-label="Today's detection summary">
        <div className="overview-copy">
          <p className="eyebrow">TODAY · OUTDOOR</p>
          <h1>See important motion<br />at a glance.</h1>
          <p>Prioritize people and moving vehicles, then join closely related clips into a single event.</p>
        </div>
        <div className="metrics">
          <article><span>Total events</span><strong>{counts.total}</strong><small>Automatically organized</small></article>
          <article><span>People</span><strong>{counts.people}</strong><small>Prioritized</small></article>
          <article><span>Vehicles</span><strong>{events.filter((event) => event.kind === "vehicle").length}</strong><small>Moving only</small></article>
          <article className="alert-metric"><span>Anomalies</span><strong>{counts.anomalies}</strong><small>Review recommended</small></article>
        </div>
      </section>

      {status?.last_error && <div className="error-banner">Last scan error: {status.last_error}</div>}

      <nav className="filters" aria-label="Detection type filters">
        {([
          ["important", "Important"],
          ["person", "People"],
          ["vehicle", "Vehicles"],
          ["anomaly", "Anomalies"],
          ["all", "All"],
        ] as const).map(([value, label]) => (
          <button key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>
            {label}
          </button>
        ))}
        <span className="interval-note">Automatic scan every 5 minutes</span>
      </nav>

      <section className="workspace">
        <div className="timeline-panel">
          <div className="section-heading">
            <div><span>EVENT TIMELINE</span><h2>{filtered.length} events</h2></div>
            <small>Newest first</small>
          </div>
          <div className="event-list">
            {filtered.map((event, index) => {
              const showDate = index === 0 || formatDate(filtered[index - 1].started_at) !== formatDate(event.started_at);
              return (
                <div key={event.id}>
                  {showDate && <p className="date-divider">{formatDate(event.started_at)}</p>}
                  <button
                    className={`event-row ${selected?.id === event.id ? "selected" : ""}`}
                    onClick={() => setSelectedId(event.id)}
                  >
                    <span className="event-time">{formatTime(event.started_at)}</span>
                    <span className={`event-icon ${event.kind}`} aria-hidden="true">
                      {event.kind === "person" ? "P" : event.kind === "vehicle" ? "■" : "≈"}
                    </span>
                    <span className="event-copy">
                      <strong>{kindLabel(event.kind)} detected</strong>
                      <span>{event.clip_ids.length > 1 ? `${event.clip_ids.length} clips joined` : event.camera}</span>
                    </span>
                    {event.anomaly && <span className="anomaly-badge">Review</span>}
                    <span className="confidence">{event.score ? `${Math.round(event.score * 100)}%` : "Analyzed"}</span>
                  </button>
                </div>
              );
            })}
            {!filtered.length && <div className="empty-state">No events match this filter.</div>}
          </div>
        </div>

        <aside className="viewer-panel">
          {selected ? (
            <>
              <div className="viewer-frame">
                {selected.video_path && selected.id > 0 ? (
                  <video key={selected.id} controls autoPlay muted playsInline onEnded={playNext} src={`${API}/media/${selected.id}`} />
                ) : (
                  <div className="camera-placeholder">
                    <div className="scan-lines" />
                    <span className={`large-marker ${selected.kind}`}>{selected.kind === "person" ? "P" : selected.kind === "vehicle" ? "■" : "≈"}</span>
                    <p>{backendOnline ? "No playable video is available for this event" : "Demo preview before live video is connected"}</p>
                  </div>
                )}
                <span className="camera-label">● {selected.camera}</span>
                <span className="timestamp-label">{formatTime(selected.started_at)}</span>
              </div>
              <div className="viewer-details">
                <div className="detail-title">
                  <div>
                    <span>{formatDate(selected.started_at)} · {formatTime(selected.started_at)}</span>
                    <h2>{kindLabel(selected.kind)} activity</h2>
                  </div>
                  <span className={`priority ${selected.anomaly ? "warn" : "normal"}`}>{selected.anomaly ? "Review needed" : "Routine activity"}</span>
                </div>
                <div className="tags">
                  {Object.entries(selected.labels).map(([label, count]) => <span key={label}>{kindLabel(label)} {count > 1 ? count : ""}</span>)}
                  {selected.clip_ids.length > 1 && <span>{selected.clip_ids.length} videos merged</span>}
                </div>
                {selected.anomaly_reasons.length > 0 ? (
                  <div className="reason-box"><strong>Anomaly assessment</strong><p>{selected.anomaly_reasons.join(" · ")}</p></div>
                ) : (
                  <div className="reason-box quiet"><strong>AI assessment</strong><p>Activity was consistently classified as a person or moving vehicle.</p></div>
                )}
                <div className="viewer-footer">
                  <span>Detection confidence</span>
                  <div className="score-track"><i style={{ width: `${Math.max(8, selected.score * 100)}%` }} /></div>
                  <strong>{selected.score ? `${Math.round(selected.score * 100)}%` : "—"}</strong>
                </div>
              </div>
            </>
          ) : <div className="empty-state">Select an event from the timeline.</div>}
        </aside>
      </section>

      <footer>
        <span>Videos and Blink credentials remain on this computer.</span>
        <span>Korea Standard Time (KST) · YOLO11n · Scan interval {Math.round((status?.interval_seconds || 300) / 60)} min</span>
      </footer>
    </main>
  );
}
