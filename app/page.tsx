"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

type EventKind = "person" | "animal" | "vehicle" | "motion" | "noise";

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
    anomaly_reasons: ["짧은 시간 반복 활동"],
    labels: { person: 1 },
    clip_ids: [1, 2, 3],
    video_path: null,
  },
  {
    id: -2,
    camera: "Outdoor",
    started_at: "2026-07-16T17:53:00-04:00",
    ended_at: "2026-07-16T17:53:00-04:00",
    kind: "animal",
    score: 0.89,
    anomaly: false,
    anomaly_reasons: [],
    labels: { dog: 1 },
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
    anomaly: true,
    anomaly_reasons: ["큰 미분류 움직임"],
    labels: {},
    clip_ids: [5],
    video_path: null,
  },
];

const LABELS: Record<string, string> = {
  person: "사람",
  animal: "동물",
  vehicle: "차량",
  motion: "미분류 움직임",
  noise: "잡음",
  dog: "개",
  cat: "고양이",
  bird: "새",
  bear: "곰",
};

function kindLabel(kind: string) {
  return LABELS[kind] || kind;
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Seoul",
  }).format(new Date(value));
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("ko-KR", {
    month: "long",
    day: "numeric",
    weekday: "short",
    timeZone: "Asia/Seoul",
  }).format(new Date(value));
}

function relativeTime(value: string | null) {
  if (!value) return "아직 없음";
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60_000));
  if (minutes < 1) return "방금 전";
  if (minutes < 60) return `${minutes}분 전`;
  return `${Math.floor(minutes / 60)}시간 전`;
}

export default function Home() {
  const [events, setEvents] = useState<MonitorEvent[]>(DEMO_EVENTS);
  const [status, setStatus] = useState<Status | null>(null);
  const [selectedId, setSelectedId] = useState<number>(DEMO_EVENTS[0].id);
  const [filter, setFilter] = useState<"important" | "person" | "animal" | "anomaly" | "all">("important");
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
        if (filter === "important") return event.kind === "person" || event.kind === "animal" || event.anomaly;
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
            <span>로컬 AI 카메라 모니터</span>
          </div>
        </div>
        <div className="system-state">
          <span className={`pulse ${backendOnline ? "online" : "demo"}`} />
          <div>
            <strong>{backendOnline ? (status?.configured ? "Blink 연결됨" : "로컬 모드") : "데모 화면"}</strong>
            <span>{backendOnline ? `마지막 확인 ${relativeTime(status?.last_scan || null)}` : "백엔드를 실행하면 실제 데이터로 전환"}</span>
          </div>
          <button className="scan-button" onClick={scanNow} disabled={!backendOnline || scanPending}>
            {scanPending ? "확인 중…" : "지금 확인"}
          </button>
        </div>
      </header>

      <section className="overview" aria-label="오늘의 감지 요약">
        <div className="overview-copy">
          <p className="eyebrow">TODAY · OUTDOOR</p>
          <h1>중요한 움직임만<br />한눈에 확인하세요.</h1>
          <p>사람과 동물을 먼저 보여주고, 가까운 시간의 클립은 하나의 사건으로 연결합니다.</p>
        </div>
        <div className="metrics">
          <article><span>전체 사건</span><strong>{counts.total}</strong><small>자동 정리됨</small></article>
          <article><span>사람</span><strong>{counts.people}</strong><small>우선 보관</small></article>
          <article><span>동물</span><strong>{counts.animals}</strong><small>우선 보관</small></article>
          <article className="alert-metric"><span>이상징후</span><strong>{counts.anomalies}</strong><small>검토 필요</small></article>
        </div>
      </section>

      {status?.last_error && <div className="error-banner">최근 확인 중 오류: {status.last_error}</div>}

      <nav className="filters" aria-label="감지 유형 필터">
        {([
          ["important", "중요 항목"],
          ["person", "사람"],
          ["animal", "동물"],
          ["anomaly", "이상징후"],
          ["all", "전체"],
        ] as const).map(([value, label]) => (
          <button key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>
            {label}
          </button>
        ))}
        <span className="interval-note">5분 간격 자동 확인</span>
      </nav>

      <section className="workspace">
        <div className="timeline-panel">
          <div className="section-heading">
            <div><span>EVENT TIMELINE</span><h2>{filtered.length}개의 사건</h2></div>
            <small>최신순</small>
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
                      {event.kind === "person" ? "人" : event.kind === "animal" ? "●" : event.kind === "vehicle" ? "■" : "≈"}
                    </span>
                    <span className="event-copy">
                      <strong>{kindLabel(event.kind)} 감지</strong>
                      <span>{event.clip_ids.length > 1 ? `${event.clip_ids.length}개 클립 연결` : event.camera}</span>
                    </span>
                    {event.anomaly && <span className="anomaly-badge">주의</span>}
                    <span className="confidence">{event.score ? `${Math.round(event.score * 100)}%` : "분석"}</span>
                  </button>
                </div>
              );
            })}
            {!filtered.length && <div className="empty-state">이 조건에 맞는 사건이 없습니다.</div>}
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
                    <span className={`large-marker ${selected.kind}`}>{selected.kind === "person" ? "人" : selected.kind === "animal" ? "●" : "≈"}</span>
                    <p>{backendOnline ? "이 사건에는 재생 가능한 영상이 없습니다" : "실제 영상 연결 전 데모 미리보기"}</p>
                  </div>
                )}
                <span className="camera-label">● {selected.camera}</span>
                <span className="timestamp-label">{formatTime(selected.started_at)}</span>
              </div>
              <div className="viewer-details">
                <div className="detail-title">
                  <div>
                    <span>{formatDate(selected.started_at)} · {formatTime(selected.started_at)}</span>
                    <h2>{kindLabel(selected.kind)} 활동</h2>
                  </div>
                  <span className={`priority ${selected.anomaly ? "warn" : "normal"}`}>{selected.anomaly ? "검토 필요" : "일반 활동"}</span>
                </div>
                <div className="tags">
                  {Object.entries(selected.labels).map(([label, count]) => <span key={label}>{kindLabel(label)} {count > 1 ? count : ""}</span>)}
                  {selected.clip_ids.length > 1 && <span>{selected.clip_ids.length}개 영상 병합</span>}
                </div>
                {selected.anomaly_reasons.length > 0 ? (
                  <div className="reason-box"><strong>이상징후 판단</strong><p>{selected.anomaly_reasons.join(" · ")}</p></div>
                ) : (
                  <div className="reason-box quiet"><strong>AI 판단</strong><p>사람 또는 동물로 안정적으로 분류된 활동입니다.</p></div>
                )}
                <div className="viewer-footer">
                  <span>감지 신뢰도</span>
                  <div className="score-track"><i style={{ width: `${Math.max(8, selected.score * 100)}%` }} /></div>
                  <strong>{selected.score ? `${Math.round(selected.score * 100)}%` : "—"}</strong>
                </div>
              </div>
            </>
          ) : <div className="empty-state">왼쪽에서 사건을 선택하세요.</div>}
        </aside>
      </section>

      <footer>
        <span>영상과 Blink 인증정보는 이 컴퓨터에만 저장됩니다.</span>
        <span>한국시간(KST) · YOLO11n / 검사 주기 · {Math.round((status?.interval_seconds || 300) / 60)}분</span>
      </footer>
    </main>
  );
}
