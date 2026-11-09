// services/ui/src/useLive.js
//
// Live connection to the API WebSocket (/api/ws) with automatic reconnect.
//
// The UI must never show an old number as if it were live. `link` says how far the chain works:
//   "api"      no connection to the API (backend down / network)
//   "bus"      API up, MQTT broker down
//   "offline"  the vision service published its last will (process stopped => relay de-energized)
//   "stale"    no fresh state for STALE_MS (vision hung, or only a retained old message)
//   "ok"       live
import { useEffect, useRef, useState, useCallback } from "react";

const STALE_MS = 2500;
const MAX_EVENTS = 200;

export function useLive() {
  const [state, setState] = useState(null);
  const [wsOpen, setWsOpen] = useState(false);
  const [busUp, setBusUp] = useState(false);
  const [fresh, setFresh] = useState(false);
  const [events, setEvents] = useState([]);
  const [crates, setCrates] = useState(null);
  const lastLive = useRef(0);
  const listeners = useRef(new Set());

  const onEvent = useCallback((fn) => {
    listeners.current.add(fn);
    return () => listeners.current.delete(fn);
  }, []);

  useEffect(() => {
    let ws;
    let stopped = false;
    let retry = 1000;
    let timer;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/api/ws`);
      ws.onopen = () => {
        retry = 1000;
        setWsOpen(true);
      };
      ws.onclose = () => {
        setWsOpen(false);
        setBusUp(false);
        if (!stopped) timer = setTimeout(connect, retry);
        retry = Math.min(retry * 2, 5000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (m) => {
        let msg;
        try {
          msg = JSON.parse(m.data);
        } catch {
          return;
        }
        switch (msg.type) {
          case "hello":
          case "ping":
            setBusUp(!!msg.bus);
            break;
          case "bus":
            setBusUp(!!msg.connected);
            break;
          case "state":
            setBusUp(true);
            setState(msg.data);
            if (msg.live) {
              lastLive.current = Date.now();
              setFresh(true);
            }
            break;
          case "event": {
            const ev = { ...msg.data, rx: Date.now() };
            if (ev.event !== "count") setEvents((list) => [ev, ...list].slice(0, MAX_EVENTS));
            listeners.current.forEach((fn) => fn(ev));
            break;
          }
          case "ack":
            break;
          case "crates":
            setCrates(msg.data.crates);
            break;
          default:
            break;
        }
      };
    };
    connect();

    const watchdog = setInterval(() => {
      setFresh(Date.now() - lastLive.current <= STALE_MS);
    }, 500);

    return () => {
      stopped = true;
      clearTimeout(timer);
      clearInterval(watchdog);
      if (ws) ws.close();
    };
  }, []);

  let link = "ok";
  if (!wsOpen) link = "api";
  else if (!busUp) link = "bus";
  else if (state && state.state === "OFFLINE") link = "offline";
  else if (!fresh) link = "stale";

  return { state, link, events, crates, setCrates, onEvent };
}
