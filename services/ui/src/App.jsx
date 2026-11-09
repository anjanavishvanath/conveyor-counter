// services/ui/src/App.jsx — kiosk shell: header, alarm banner, three screens, dialogs, toasts.
import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { useLive } from "./useLive.js";
import { LINK_TEXT, faultText } from "./text.js";
import Header from "./components/Header.jsx";
import Production from "./components/Production.jsx";
import Reports from "./components/Reports.jsx";
import SystemView from "./components/SystemView.jsx";
import { Toasts } from "./components/Overlays.jsx";

function AlarmBanner({ state, link, act }) {
  if (link !== "ok") {
    return (
      <div className="alarm alarm-bad" role="alert">
        <span className="alarm-icon">!</span>
        <span>{LINK_TEXT[link]}</span>
      </div>
    );
  }
  if (state?.state === "FAULT") {
    return (
      <div className="alarm alarm-bad alarm-flash" role="alert">
        <span className="alarm-icon">!</span>
        <span>
          FAULT: conveyor stopped. {(state.fault_reasons || []).map(faultText).join(" · ")}
        </span>
        <button className="btn btn-warn" disabled={!state.camera_ok} onClick={() => act(() => api.command("reset_fault"), "Fault reset")}>
          Reset fault
        </button>
      </div>
    );
  }
  if (state?.stop_button_active) {
    return (
      <div className="alarm alarm-warn" role="status">
        <span className="alarm-icon">■</span>
        <span>Stop button is pressed (or its circuit is open). The conveyor cannot start.</span>
      </div>
    );
  }
  if (state?.batch?.uncertain && ["READY", "PAUSED", "RUNNING", "CLEARING"].includes(state.state)) {
    return (
      <div className="alarm alarm-warn" role="status">
        <span className="alarm-icon">?</span>
        <span>This batch had a fault or restart. Check the count before trusting it.</span>
      </div>
    );
  }
  return null;
}

let toastId = 0;

export default function App() {
  const live = useLive();
  const { state, link, events, crates, setCrates, onEvent } = live;
  const [info, setInfo] = useState(null);
  const [tab, setTab] = useState("production");
  const [dialog, setDialog] = useState(null);
  const [toasts, setToasts] = useState([]);
  const [reportKey, setReportKey] = useState(0);
  const timers = useRef({});

  const toast = useCallback((text, tone = "ok") => {
    const id = ++toastId;
    setToasts((t) => [...t, { id, text, tone }].slice(-4));
    timers.current[id] = setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "bad" ? 8000 : 4000);
  }, []);
  const dismiss = useCallback((id) => setToasts((t) => t.filter((x) => x.id !== id)), []);

  /** Run an API call; toast the vision service's answer. Returns the {ok, reason, data} result. */
  const act = useCallback(
    async (fn, success) => {
      const r = await fn();
      if (!r.ok) toast(r.reason || "Refused", "bad");
      else if (success) toast(typeof success === "function" ? success(r) : success, "ok");
      if (r.ok && r.data && r.data.crates) setCrates(r.data.crates);
      return r;
    },
    [toast, setCrates],
  );

  const loadInfo = useCallback(async () => {
    const r = await api.info();
    if (r.ok) setInfo(r.data);
    return r.ok;
  }, []);
  const loadCrates = useCallback(async () => {
    const r = await api.crates();
    if (r.ok) setCrates(r.data.crates);
  }, [setCrates]);

  // info + crates on start, retried every 5 s until they load, and again whenever the live link
  // comes back (the backend may have restarted with a changed site.yaml)
  const backendUp = link !== "api";
  useEffect(() => {
    let done = false;
    const load = async () => {
      if (done) return;
      const ok = await loadInfo();
      if (ok) {
        await loadCrates();
        done = true;
      }
    };
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [backendUp, loadInfo, loadCrates]);

  // refresh crate tiles + reports when a batch closes; refresh periodically for the day rollover
  useEffect(
    () =>
      onEvent((e) => {
        if (["batch_completed", "batch_aborted", "batch_recovered"].includes(e.event)) {
          loadCrates();
          setReportKey((k) => k + 1);
        }
      }),
    [onEvent, loadCrates],
  );
  useEffect(() => {
    const t = setInterval(loadCrates, 60000);
    return () => clearInterval(t);
  }, [loadCrates]);

  // Kiosk: return to the production screen after 3 minutes without a touch.
  useEffect(() => {
    if (tab === "production") return;
    let t = setTimeout(() => setTab("production"), 180000);
    const reset = () => {
      clearTimeout(t);
      t = setTimeout(() => setTab("production"), 180000);
    };
    window.addEventListener("pointerdown", reset);
    return () => {
      clearTimeout(t);
      window.removeEventListener("pointerdown", reset);
    };
  }, [tab]);

  const common = { info, state, link, act, setDialog };

  return (
    <div className="app">
      <Header info={info} state={state} link={link} tab={tab} setTab={setTab} />
      <AlarmBanner state={state} link={link} act={act} />
      <main className="content">
        {tab === "production" && <Production {...common} crates={crates} />}
        {tab === "reports" && <Reports act={act} refreshKey={reportKey} />}
        {tab === "system" && <SystemView {...common} events={events} />}
      </main>
      {dialog}
      <Toasts items={toasts} onDismiss={dismiss} />
    </div>
  );
}
