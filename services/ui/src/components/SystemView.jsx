// System: diagnostics for the supervisor/technician, event log, and board power actions.
import React, { useState } from "react";
import { api } from "../api.js";
import { LINK_TEXT, faultText } from "../text.js";
import { Confirm } from "./Overlays.jsx";

function Row({ k, v, tone }) {
  return (
    <div className={`diag-row ${tone ? `diag-${tone}` : ""}`}>
      <dt>{k}</dt>
      <dd>{v}</dd>
    </div>
  );
}

function eventText(e) {
  switch (e.event) {
    case "state_changed":
      return `${e.old} → ${e.new}${e.reason ? ` (${e.reason})` : ""}`;
    case "fault":
      return `FAULT: ${faultText(e.reason)}`;
    case "fault_cleared":
      return "Fault reset";
    case "command_rejected":
      return `Refused "${e.command}": ${e.reason}`;
    case "batch_selected":
      return `Crate ${e.batch?.crate} selected, target ${e.batch?.target}`;
    case "batch_completed":
      return `Batch complete: crate ${e.batch?.crate}, ${e.batch?.count}/${e.batch?.target}`;
    case "batch_aborted":
      return `Batch cancelled: crate ${e.batch?.crate}, ${e.batch?.count} counted`;
    case "batch_recovered":
      return `Batch recovered after restart: crate ${e.batch?.crate}, ${e.batch?.count} counted (review)`;
    case "target_reached":
      return `Target reached: ${e.count}/${e.target}`;
    case "stop_button":
      return e.active ? "Stop button pressed" : "Stop button released";
    case "unassigned_counts":
      return `${e.added} coconut(s) counted with no batch running (total ${e.total_unassigned})`;
    default:
      return e.event;
  }
}

export default function SystemView({ info, state, link, events, act, setDialog }) {
  const s = state || {};
  const live = link === "ok";
  const sd = s.suspect_detail || {};
  const [busy, setBusy] = useState(false);
  const allowed = info?.system_control;
  const busyConveyor = live && ["RUNNING", "CLEARING"].includes(s.state);

  const power = (action, title, message) =>
    setDialog(
      <Confirm
        title={title}
        message={message}
        confirmLabel={title}
        tone="danger"
        busy={busy}
        onClose={() => setDialog(null)}
        onConfirm={async () => {
          setBusy(true);
          await act(() => api.system(action), `${title}: command sent`);
          setBusy(false);
          setDialog(null);
        }}
      />,
    );

  const powerNote = !allowed
    ? "Disabled in site.yaml (api.allow_system_control)"
    : busyConveyor
      ? "Stop the conveyor and finish the batch first"
      : null;

  return (
    <div className="system">
      <section className="panel">
        <div className="panel-head">
          <span>Live status</span>
          {!live && <span className="tag tag-bad">{LINK_TEXT[link]}</span>}
        </div>
        <dl className="diag">
          <Row k="Controller state" v={live ? s.state : "unknown"} />
          <Row k="Run permit (relay)" v={live ? (s.run_permit ? "ON: conveyor allowed" : "OFF: conveyor stopped") : "unknown"} tone={live && s.run_permit ? "ok" : undefined} />
          <Row k="Alarm light" v={live ? (s.alarm_light ? "ON" : "OFF") : "unknown"} />
          <Row k="Stop button" v={live ? (s.stop_button_active ? "PRESSED / open circuit" : "released") : "unknown"} tone={s.stop_button_active ? "warn" : undefined} />
          <Row k="Camera" v={live ? (s.camera_ok ? "frames arriving" : "NO FRAMES") : "unknown"} tone={live && !s.camera_ok ? "bad" : undefined} />
          <Row k="Processing rate" v={live ? `${s.fps} fps` : "—"} />
          <Row k="Inference time" v={live ? `${s.infer_ms} ms per frame` : "—"} />
          <Row k="Detector" v={`${s.detector ?? info?.detector ?? "—"} on ${s.device ?? "—"}`} tone={s.device === "replay" ? "warn" : undefined} />
          <Row k="Frames processed" v={s.frames?.toLocaleString() ?? "—"} />
          <Row k="Database queue" v={s.store_backlog != null ? `${s.store_backlog} pending, ${s.store_dropped} dropped` : "—"} tone={s.store_dropped ? "bad" : undefined} />
          <Row k="Counted since service start" v={s.lifetime_total ?? "—"} />
          <Row k="Counted with no batch running" v={s.unassigned_count ?? "—"} tone={s.unassigned_count ? "warn" : undefined} />
          <Row
            k="Suspect events"
            v={
              s.suspects != null
                ? `${s.suspects} (first seen past line ${sd.born_downstream ?? 0}, lost near line ${sd.lost_upstream_near_line ?? 0}, backward ${sd.backward_crossings ?? 0}, unconfirmed ${sd.crossed_unconfirmed ?? 0})`
                : "—"
            }
            tone={s.suspects ? "warn" : undefined}
          />
        </dl>
      </section>

      <section className="panel">
        <div className="panel-head">
          <span>Site</span>
        </div>
        <dl className="diag">
          <Row k="Site" v={info?.site_name ?? "—"} />
          <Row k="Product" v={info?.product ?? "—"} />
          <Row k="Crates" v={info ? `${info.crates} (max target ${info.max_target})` : "—"} />
          <Row k="Frame source" v={info?.frame_source ?? "—"} tone={info?.frame_source === "video_file" ? "warn" : undefined} />
          <Row k="IO" v={info?.io === "sim" ? "simulated (no relay)" : info?.io ?? "—"} tone={info?.io === "sim" ? "warn" : undefined} />
          <Row k="Time zone" v={info?.timezone ?? "—"} />
        </dl>
        <div className="panel-head">
          <span>Power</span>
        </div>
        <div className="power">
          <button className="btn btn-lg" disabled={!!powerNote} title={powerNote || ""} onClick={() => power("restart", "Restart counter", "The counting service restarts. The conveyor stays stopped until START is pressed.")}>
            Restart counter
          </button>
          <button className="btn btn-lg" disabled={!!powerNote} title={powerNote || ""} onClick={() => power("reboot", "Reboot", "The controller restarts. The conveyor stays stopped until START is pressed.")}>
            Reboot
          </button>
          <button className="btn btn-lg btn-danger" disabled={!!powerNote} title={powerNote || ""} onClick={() => power("shutdown", "Shut down", "The controller powers off. Wait until the screen is dark before switching off the panel.")}>
            Shut down
          </button>
        </div>
        {powerNote && <div className="action-hint">{powerNote}</div>}
      </section>

      <section className="panel events">
        <div className="panel-head">
          <span>Event log (this session)</span>
        </div>
        <ol className="event-list">
          {events.length === 0 && <li className="muted">No events yet</li>}
          {events.map((e, i) => (
            <li key={`${e.ts}-${i}`} className={e.event === "fault" ? "ev-bad" : e.event === "command_rejected" || e.event === "unassigned_counts" ? "ev-warn" : ""}>
              <span className="ev-time">{new Date((e.ts || e.rx / 1000) * 1000).toLocaleTimeString([], { hour12: false })}</span>
              <span>{eventText(e)}</span>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
