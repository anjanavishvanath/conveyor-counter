// Reports: one production day at a time (the day boundary is site.yaml reporting.day_start).
import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { faultText, fmtDuration, fmtTime, stopText } from "../text.js";

function addDays(iso, n) {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}

function prettyDay(iso) {
  return new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

export default function Reports({ act, refreshKey }) {
  const [day, setDay] = useState(null); // null = today (server's production day)
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [usb, setUsb] = useState(null);
  const [exporting, setExporting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const r = await api.dayReport(day);
    setLoading(false);
    if (r.ok) {
      setData(r.data);
      setError(null);
    } else setError(r.reason);
  }, [day]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  useEffect(() => {
    api.usb().then((r) => r.ok && setUsb(r.data));
    const t = setInterval(() => api.usb().then((r) => r.ok && setUsb(r.data)), 5000);
    return () => clearInterval(t);
  }, []);

  const shown = data?.summary?.day;
  const today = data?.today;
  const s = data?.summary;
  const batches = data?.batches || [];
  const usbReady = usb && usb.mounts.length > 0;

  const doExport = async () => {
    setExporting(true);
    await act(
      () => api.exportCsv(shown),
      (r) => `${r.data.rows} batch(es) saved ${r.data.where === "usb" ? "to USB" : "on this controller"}: ${r.data.path}`,
    );
    setExporting(false);
  };

  return (
    <div className="reports">
      <section className="panel report-bar">
        <div className="day-nav">
          <button className="btn btn-lg" onClick={() => setDay(addDays(shown, -1))} disabled={!shown}>
            ◀ Previous day
          </button>
          <div className="day-label">
            <div className="day-title">{shown ? prettyDay(shown) : "…"}</div>
            <div className="day-sub">{shown === today ? "Today (production day)" : "Production day"}{s ? ` · starts ${s.day_start}` : ""}</div>
          </div>
          <button className="btn btn-lg" onClick={() => setDay(addDays(shown, 1))} disabled={!shown || shown >= today}>
            Next day ▶
          </button>
          <button className="btn btn-lg" onClick={() => setDay(null)} disabled={!shown || shown === today}>
            Today
          </button>
        </div>
        <div className="export">
          <button className="btn btn-lg btn-primary" onClick={doExport} disabled={exporting || !shown}>
            {exporting ? "Saving…" : usbReady ? "Copy CSV to USB" : "Save CSV"}
          </button>
          <a className="btn btn-lg" href={api.csvUrl(shown)} download>
            Download CSV
          </a>
          <div className="export-hint">{usbReady ? `USB: ${usb.mounts[0]}` : `No USB stick found. Saves to ${usb?.fallback_dir ?? "the export folder"}`}</div>
        </div>
      </section>

      {error && <div className="banner banner-bad">{error}</div>}
      {data && !data.db_ready && <div className="banner banner-info">No production records yet. The vision service creates the database on its first start.</div>}

      {s && (
        <section className="kpis">
          <div className="kpi">
            <div className="kpi-label">Batches completed</div>
            <div className="kpi-value">{s.batches}</div>
          </div>
          <div className="kpi">
            <div className="kpi-label">Coconuts counted</div>
            <div className="kpi-value">{s.counted.toLocaleString()}</div>
          </div>
          <div className="kpi">
            <div className="kpi-label">Target total</div>
            <div className="kpi-value">{s.targeted.toLocaleString()}</div>
          </div>
          <div className={`kpi ${s.uncertain_batches ? "kpi-warn" : ""}`}>
            <div className="kpi-label">Need review</div>
            <div className="kpi-value">{s.uncertain_batches}</div>
          </div>
        </section>
      )}

      {s && s.per_crate.length > 0 && (
        <section className="panel">
          <div className="panel-head">
            <span>Per crate (completed batches)</span>
          </div>
          <div className="per-crate">
            {s.per_crate.map((p) => (
              <div key={p.crate} className="per-crate-item">
                <div className="muted">Crate {p.crate}</div>
                <div className="per-crate-value">{p.counted}</div>
                <div className="muted small">{p.batches} batch{p.batches === 1 ? "" : "es"}</div>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="panel table-panel">
        <div className="panel-head">
          <span>Batches {loading && <span className="muted small">updating…</span>}</span>
          <button className="btn btn-sm" onClick={load}>
            Refresh
          </button>
        </div>
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Selected</th>
                <th>Crate</th>
                <th className="num">Target</th>
                <th className="num">Count</th>
                <th className="num">Over</th>
                <th>Started</th>
                <th>Finished</th>
                <th className="num">Duration</th>
                <th>Status</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {batches.length === 0 && (
                <tr>
                  <td colSpan={10} className="muted center">
                    No batches on this day
                  </td>
                </tr>
              )}
              {batches.map((b) => (
                <tr key={b.batch_id} className={b.uncertain ? "row-warn" : ""}>
                  <td>{fmtTime(b.created_at)}</td>
                  <td>{b.crate}</td>
                  <td className="num">{b.target}</td>
                  <td className="num strong">
                    {b.count}
                    {b.corrections ? <span className="muted small"> ({b.corrections > 0 ? "+" : ""}{b.corrections})</span> : null}
                  </td>
                  <td className="num">{b.overfill || ""}</td>
                  <td>{fmtTime(b.started_at)}</td>
                  <td>{fmtTime(b.completed_at)}</td>
                  <td className="num">{b.duration_s != null ? fmtDuration(b.duration_s) : "—"}</td>
                  <td>
                    <span className={`bstatus bstatus-${b.status}`}>{b.status === "aborted" ? "cancelled" : b.status}</span>
                    {b.uncertain && <span className="tag tag-warn">REVIEW</span>}
                  </td>
                  <td className="notes">
                    {[...(b.faults || []).map(faultText), ...(b.stops || []).filter((x) => x !== "target").map(stopText)].join(" · ")}
                    {b.frames_lost ? ` · ${b.frames_lost} frames lost` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
