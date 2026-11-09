// services/ui/src/api.js — REST calls. Every call resolves to {ok, reason, data}; it never throws,
// so a button handler can always show the operator what happened.

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    return { ok: false, reason: "No connection to the counter backend" };
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* empty or non-JSON body */
  }
  if (!res.ok) {
    const detail = data && data.detail;
    const reason = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : `HTTP ${res.status}`;
    return { ok: false, reason, data };
  }
  // Batch commands answer {ok, reason} from the vision service itself.
  if (data && typeof data.ok === "boolean") return { ok: data.ok, reason: data.reason || "", data };
  return { ok: true, reason: "", data };
}

export const api = {
  info: () => request("GET", "/api/info"),
  crates: () => request("GET", "/api/crates"),
  setPreset: (crate, target) => request("PUT", `/api/presets/${crate}`, { target }),
  setAllPresets: (target) => request("PUT", "/api/presets", { target }),
  selectCrate: (crate, target) => request("POST", "/api/batch/select", target ? { crate, target } : { crate }),
  command: (name) => request("POST", `/api/command/${name}`),
  dayReport: (day) => request("GET", `/api/reports/day${day ? `?day=${day}` : ""}`),
  exportCsv: (day) => request("POST", "/api/reports/export", { day }),
  usb: () => request("GET", "/api/usb"),
  system: (action) => request("POST", `/api/system/${action}`),
  csvUrl: (day) => `/api/reports/day.csv${day ? `?day=${day}` : ""}`,
};
