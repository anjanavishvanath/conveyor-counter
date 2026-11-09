import React, { useEffect, useState } from "react";

function Clock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="clock">
      <div className="clock-time">{now.toLocaleTimeString([], { hour12: false })}</div>
      <div className="clock-date">
        {now.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" })}
      </div>
    </div>
  );
}

function Pill({ ok, warn, label, value }) {
  const tone = ok ? "ok" : warn ? "warn" : "bad";
  return (
    <div className={`pill pill-${tone}`} title={value}>
      <span className="pill-dot" />
      <span className="pill-label">{label}</span>
    </div>
  );
}

export default function Header({ info, state, link, tab, setTab }) {
  const live = link === "ok";
  const s = state || {};
  const devMode = info && (info.detector === "replay" || info.io === "sim" || info.frame_source === "video_file");
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-title">{info ? info.site_name : "Conveyor Counter"}</div>
        <div className="brand-sub">
          {info ? `Product: ${info.product}` : "connecting…"}
          {devMode && <span className="dev-tag" title={`source ${info.frame_source}, detector ${info.detector}, IO ${info.io}`}>TEST MODE</span>}
        </div>
      </div>

      <nav className="tabs" role="tablist">
        {[
          ["production", "Production"],
          ["reports", "Reports"],
          ["system", "System"],
        ].map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} className={`tab ${tab === id ? "is-active" : ""}`} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
      </nav>

      <div className="status">
        <Pill ok={link !== "api"} label="Backend" value={link === "api" ? "no connection" : "connected"} />
        <Pill ok={link !== "api" && link !== "bus"} label="Bus" value={link === "bus" ? "MQTT down" : "MQTT ok"} />
        <Pill ok={live} warn={link === "stale"} label="Vision" value={live ? `${s.fps ?? "–"} fps` : link} />
        <Pill ok={live && s.camera_ok} label="Camera" value={live && s.camera_ok ? "frames arriving" : "no frames"} />
        <Clock />
      </div>
    </header>
  );
}
