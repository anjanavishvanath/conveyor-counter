// Production screen: the active batch (left) and the crate grid (right).
import React, { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { STATE_TEXT, faultText, fmtDuration, fmtTime, stopText } from "../text.js";
import { Confirm, Keypad } from "./Overlays.jsx";

// ───────────────────────── rate (coconuts / minute, rolling 60 s) ─────────────────────────
function useRate(batchId, count) {
  const samples = useRef([]);
  const [rate, setRate] = useState(null);
  useEffect(() => {
    samples.current = [];
    setRate(null);
  }, [batchId]);
  useEffect(() => {
    if (count == null) return;
    const now = Date.now();
    const s = samples.current;
    if (!s.length || s[s.length - 1].c !== count) s.push({ t: now, c: count });
    while (s.length > 2 && now - s[0].t > 60000) s.shift();
    const span = s.length > 1 ? s[s.length - 1].t - s[0].t : 0;
    setRate(span >= 10000 ? ((s[s.length - 1].c - s[0].c) / span) * 60000 : null);
  }, [count]);
  return rate;
}

// ───────────────────────── live preview ─────────────────────────
function Preview({ info, live }) {
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const [show, setShow] = useState(true);
  useEffect(() => {
    if (live && failed) {
      const t = setTimeout(() => {
        setFailed(false);
        setAttempt((a) => a + 1);
      }, 3000);
      return () => clearTimeout(t);
    }
  }, [live, failed]);
  if (!info || !info.preview || !info.preview.enabled) return null;
  const host = ["127.0.0.1", "localhost", "0.0.0.0"].includes(info.preview.host) ? location.hostname : info.preview.host;
  const src = `${location.protocol}//${host}:${info.preview.port}/preview.mjpg?a=${attempt}`;
  return (
    <section className="panel preview">
      <div className="panel-head">
        <span>Camera view</span>
        <button className="btn btn-sm btn-ghost" onClick={() => setShow((v) => !v)}>
          {show ? "Hide" : "Show"}
        </button>
      </div>
      {show && (
        <div className="preview-frame">
          {!failed && live ? (
            <img src={src} alt="Annotated camera view" onError={() => setFailed(true)} />
          ) : (
            <div className="preview-empty">{live ? "Preview unavailable, retrying…" : "No live camera view"}</div>
          )}
        </div>
      )}
    </section>
  );
}

// ───────────────────────── active batch ─────────────────────────
function BatchPanel({ state, link, info, act, setDialog }) {
  const live = link === "ok";
  const st = live ? state?.state : "UNKNOWN";
  const batch = state?.batch;
  const last = state?.last_batch;
  const rate = useRate(batch?.batch_id, live ? batch?.count : null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const text = STATE_TEXT[st] || { label: "NO DATA", hint: "Waiting for the vision service" };
  const tone = live ? st.toLowerCase() : "unknown";
  const count = batch?.count ?? 0;
  const target = batch?.target ?? 0;
  const pct = target ? Math.min(100, (count / target) * 100) : 0;
  const remaining = target ? Math.max(0, target - count) : null;
  const overfill = target ? Math.max(0, count - target) : 0;
  const elapsed = batch?.started_at ? now / 1000 - batch.started_at : null;
  const eta = rate && remaining ? (remaining / rate) * 60 : null;

  const canStart = live && ["READY", "PAUSED"].includes(st) && !state?.stop_button_active;
  const startBlock = !live
    ? "No live data"
    : st === "IDLE"
      ? "Select a crate first"
      : st === "FAULT"
        ? "Reset the fault first"
        : state?.stop_button_active
          ? "Stop button is pressed"
          : null;

  return (
    <section className={`panel batch tone-${tone}`}>
      <div className="batch-state">
        <div className="batch-state-label">{text.label}</div>
        <div className="batch-state-hint">
          {st === "RUNNING" && batch ? `Conveyor running, counting into crate ${batch.crate}` : text.hint}
        </div>
      </div>

      {st === "FAULT" && state?.fault_reasons?.length > 0 && (
        <ul className="fault-list">
          {state.fault_reasons.map((r) => (
            <li key={r}>{faultText(r)}</li>
          ))}
        </ul>
      )}

      {batch ? (
        <div className={`batch-body ${live ? "" : "is-stale"}`}>
          <div className="batch-crate">
            <span className="batch-crate-label">CRATE</span>
            <span className="batch-crate-no">{batch.crate}</span>
            {batch.uncertain && <span className="tag tag-warn" title={(batch.faults || []).map(faultText).join(", ")}>REVIEW COUNT</span>}
          </div>
          <div className="batch-count">
            <span className="count-value">{count}</span>
            <span className="count-target">/ {target}</span>
          </div>
          <div className="progress" role="progressbar" aria-valuenow={count} aria-valuemax={target}>
            <div className="progress-fill" style={{ width: `${pct}%` }} />
          </div>
          <dl className="stats">
            <div>
              <dt>Remaining</dt>
              <dd>{remaining ?? "—"}</dd>
            </div>
            <div>
              <dt>Overfill</dt>
              <dd className={overfill ? "warn-text" : ""}>{overfill ? `+${overfill}` : "0"}</dd>
            </div>
            <div>
              <dt>Rate</dt>
              <dd>{rate != null ? `${rate.toFixed(0)}/min` : "—"}</dd>
            </div>
            <div>
              <dt>{st === "RUNNING" && eta ? "To target" : "Elapsed"}</dt>
              <dd>{st === "RUNNING" && eta ? fmtDuration(eta) : fmtDuration(elapsed)}</dd>
            </div>
          </dl>
          {!live && <div className="stale-note">Last known values. Not live.</div>}
          {batch.stops?.length > 0 && <div className="batch-stops">Stops: {batch.stops.slice(-3).map(stopText).join(" · ")}</div>}
        </div>
      ) : (
        <div className="batch-empty">
          <div className="batch-empty-title">No batch selected</div>
          <div className="batch-empty-text">Tap a crate on the right, check its target and press “Select crate”.</div>
          {last && (
            <div className="last-batch">
              Last batch: crate {last.crate}, <b>{last.count}</b> / {last.target}
              {last.status === "aborted" ? " (cancelled)" : ""} at {fmtTime(last.completed_at)}
              {last.uncertain && <span className="tag tag-warn">REVIEW</span>}
            </div>
          )}
        </div>
      )}

      <div className="batch-actions">
        <button
          className="btn btn-start btn-xl"
          disabled={!canStart}
          title={startBlock || ""}
          onClick={() => act(() => api.command("start"), st === "PAUSED" ? "Batch resumed" : "Conveyor started")}
        >
          {st === "PAUSED" ? "RESUME" : "START"}
        </button>
        <button className="btn btn-stop btn-xl" disabled={link === "api" || link === "bus"} onClick={() => act(() => api.command("stop"), "Stop sent")}>
          STOP
        </button>
      </div>
      {!canStart && startBlock && live && st !== "RUNNING" && st !== "CLEARING" && <div className="action-hint">{startBlock}</div>}

      <div className="batch-secondary">
        {st === "CLEARING" && (
          <button
            className="btn btn-lg btn-primary"
            onClick={() =>
              setDialog(
                <Confirm
                  title="Finish batch now?"
                  message={`Only finish when the belt is empty. Otherwise the batch closes by itself ${info?.clear_time_s ?? 5} s after the last coconut is counted.`}
                  confirmLabel="Belt is clear, finish"
                  onClose={() => setDialog(null)}
                  onConfirm={() => {
                    setDialog(null);
                    act(() => api.command("finish_batch"), "Batch finished");
                  }}
                />,
              )
            }
          >
            Finish batch
          </button>
        )}
        {["READY", "PAUSED"].includes(st) && (
          <button
            className="btn btn-lg"
            onClick={() =>
              setDialog(
                <Confirm
                  title={`Cancel batch for crate ${batch?.crate}?`}
                  message={
                    count > 0
                      ? `${count} coconut(s) were already counted into this crate. The batch is saved as CANCELLED with that count.`
                      : "The batch is closed without any count."
                  }
                  confirmLabel="Cancel batch"
                  tone="danger"
                  onClose={() => setDialog(null)}
                  onConfirm={() => {
                    setDialog(null);
                    act(() => api.command("cancel_batch"), "Batch cancelled");
                  }}
                />,
              )
            }
          >
            Cancel batch
          </button>
        )}
        {st === "FAULT" && (
          <button className="btn btn-lg btn-warn" disabled={!state?.camera_ok} title={state?.camera_ok ? "" : "Camera frames still missing"} onClick={() => act(() => api.command("reset_fault"), "Fault reset")}>
            Reset fault
          </button>
        )}
      </div>
    </section>
  );
}

// ───────────────────────── crate grid ─────────────────────────
function CrateTile({ c, active, st, live, onOpen }) {
  const cls = ["crate"];
  let badge = null;
  if (active) {
    cls.push("is-active", `tone-${(st || "").toLowerCase()}`);
    badge = { READY: "SELECTED", RUNNING: "FILLING", PAUSED: "PAUSED", CLEARING: "CLEARING", FAULT: "FAULT" }[st] || "ACTIVE";
  }
  return (
    <button className={cls.join(" ")} onClick={onOpen}>
      <div className="crate-head">
        <span className="crate-no">Crate {c.crate}</span>
        {badge && <span className="crate-badge">{badge}</span>}
      </div>
      {active && live ? (
        <div className="crate-main">
          <span className="crate-count">{active.count}</span>
          <span className="crate-of">/ {active.target}</span>
        </div>
      ) : (
        <div className="crate-main">
          <span className="crate-target">{c.target}</span>
          <span className="crate-of">target</span>
        </div>
      )}
      <div className="crate-foot">
        {active ? (
          active.started_at ? <>Started {fmtTime(active.started_at)}</> : <>Selected {fmtTime(active.created_at)}, not started</>
        ) : c.last ? (
          <>
            Last: <b>{c.last.count}</b> at {fmtTime(c.last.completed_at)}
            {c.last.uncertain && <span className="tag tag-warn">REVIEW</span>}
          </>
        ) : (
          <span className="muted">No batch today</span>
        )}
      </div>
    </button>
  );
}

function CrateGrid({ crates, state, link, info, act, setDialog }) {
  const live = link === "ok";
  const st = state?.state;
  const batch = state?.batch;
  const canSelect = live && ["IDLE", "READY"].includes(st);
  const maxT = info?.max_target ?? 2000;

  const openCrate = (c) => {
    const isActive = batch && batch.crate === c.crate;
    const selectNote = !live
      ? "No live data"
      : st === "FAULT"
        ? "Reset the fault first"
        : !canSelect
          ? `A batch is in progress (${st}). Finish or cancel it first`
          : null;
    setDialog(
      <Keypad
        title={`Crate ${c.crate}`}
        subtitle={isActive ? `Active batch target: ${batch.target}. Changing the preset does not change it.` : "Target count for this crate"}
        initial={c.target}
        max={maxT}
        note={selectNote && `Select is unavailable: ${selectNote}.`}
        onClose={() => setDialog(null)}
        actions={[
          {
            label: "Save target",
            onClick: (v) => {
              setDialog(null);
              act(() => api.setPreset(c.crate, v), `Crate ${c.crate} target set to ${v}`);
            },
          },
          {
            label: st === "READY" && isActive ? "Save & reselect" : "Save & select crate",
            tone: "primary",
            disabled: !canSelect,
            note: selectNote,
            onClick: async (v) => {
              setDialog(null);
              const saved = await act(() => api.setPreset(c.crate, v), null);
              if (saved.ok) act(() => api.selectCrate(c.crate, v), `Crate ${c.crate} selected, target ${v}. Press START.`);
            },
          },
        ]}
      />,
    );
  };

  const openSetAll = () =>
    setDialog(
      <Keypad
        title="Set target for all crates"
        subtitle={`Applies to all ${crates?.length ?? ""} crates. A running batch keeps its own target.`}
        initial={info?.default_target ?? 400}
        max={maxT}
        onClose={() => setDialog(null)}
        actions={[
          {
            label: "Apply to all crates",
            tone: "primary",
            onClick: (v) => {
              setDialog(null);
              act(() => api.setAllPresets(v), `All crates set to ${v}`);
            },
          },
        ]}
      />,
    );

  return (
    <section className="panel crates">
      <div className="panel-head">
        <span>Crates</span>
        <span className="panel-head-hint">Tap a crate to change its target or select it</span>
        <button className="btn btn-sm" onClick={openSetAll} disabled={!crates}>
          Set all targets
        </button>
      </div>
      <div className="crate-grid" style={{ "--cols": (crates?.length ?? 12) > 12 ? 5 : 4 }}>
        {(crates || []).map((c) => (
          <CrateTile key={c.crate} c={c} live={live} st={st} active={batch && batch.crate === c.crate ? batch : null} onOpen={() => openCrate(c)} />
        ))}
        {!crates && <div className="muted pad">Loading crates…</div>}
      </div>
    </section>
  );
}

export default function Production(props) {
  return (
    <div className="production">
      <div className="col-left">
        <BatchPanel {...props} />
        <Preview info={props.info} live={props.link === "ok"} />
      </div>
      <CrateGrid {...props} />
    </div>
  );
}
