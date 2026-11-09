// services/ui/src/text.js — operator wording for states, faults and stop reasons.

export const STATE_TEXT = {
  IDLE: { label: "IDLE", hint: "Select a crate to prepare the next batch" },
  READY: { label: "READY", hint: "Press START to run the conveyor" },
  RUNNING: { label: "RUNNING", hint: "Conveyor running, counting into the crate" },
  PAUSED: { label: "PAUSED", hint: "Conveyor stopped. Counting continues. START resumes the batch" },
  CLEARING: { label: "TARGET REACHED", hint: "Conveyor stopped. Still counting what is on the belt" },
  FAULT: { label: "FAULT", hint: "Conveyor stopped. Clear the cause, then reset" },
  OFFLINE: { label: "OFFLINE", hint: "Vision service not running. Conveyor interlock open" },
};

export const LINK_TEXT = {
  api: "No connection to the counter backend. Live values unknown",
  bus: "Message bus (MQTT) is down. Live values unknown",
  offline: "Vision service is not running. The conveyor cannot run",
  stale: "No live data from the vision service",
};

export function faultText(reason) {
  if (!reason) return "";
  const [key, arg] = String(reason).split(":");
  switch (key) {
    case "frame_timeout":
      return "Camera: no frames";
    case "source_eos":
      return "Video source ended";
    case "source_error":
      return "Camera error";
    case "frames_lost":
      return `Camera lost ${arg} frame(s)`;
    case "low_fps":
      return `Processing too slow (${arg} fps)`;
    case "db_error":
      return "Database write failure: counts not being saved";
    case "bus_offline":
      return "Message bus offline";
    case "io_thread_stalled":
      return "I/O thread stalled";
    case "restart_during_batch":
      return "Service restarted during the batch";
    default:
      return reason;
  }
}

export function stopText(reason) {
  if (!reason) return "";
  if (reason.startsWith("fault:")) return `Fault: ${faultText(reason.slice(6))}`;
  return (
    {
      target: "Target reached",
      stop_button: "Stop button",
      ui_stop: "Stopped from screen",
      api_stop: "Stopped from screen",
      button_stop: "Stop button",
      service_restart: "Service restart",
    }[reason] || reason
  );
}

export function fmtTime(epoch) {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function fmtDuration(s) {
  if (s == null || !isFinite(s)) return "—";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(sec).padStart(2, "0")}s`;
}
