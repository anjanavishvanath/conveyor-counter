# MQTT contract

The bus between the vision service, the API and the UI. Broker: mosquitto on localhost.
Base topic from `config/site.yaml` → `mqtt.base_topic` (default `counter`).

**The bus is not a control path.** Counting and the run-permit interlock live inside the
vision service and keep working with the broker down. Losing the broker only blinds the UI.
Whether that should stop the conveyor is a plant decision: `safety.fault_on_bus_loss`
(default `false`, with `safety.bus_grace_s` for the reconnect window).

Only the vision service publishes `state`, `event` and `ack`. Only the API publishes `cmd/*`.

## `counter/state` — retained, ~5 Hz

The whole picture, so a UI that connects late renders correctly from the first message.

```json
{
  "state": "RUNNING",
  "run_permit": true,
  "alarm_light": false,
  "stop_button_active": false,
  "camera_ok": true,
  "fps": 28.7,
  "infer_ms": 11.4,
  "frames": 184203,
  "fault_reasons": [],
  "unassigned_count": 0,
  "batch": {
    "batch_id": "4f1c…", "crate": 3, "target": 400, "count": 271,
    "status": "open", "created_at": 1760000000.0, "started_at": 1760000012.0,
    "target_reached_at": null, "completed_at": null,
    "uncertain": false, "frames_lost": 0, "faults": [], "stops": []
  },
  "last_batch": { "…": "the previous batch, for the 'last crate' tile" },
  "site_name": "Coconut plant - line 1",
  "product": "coconut",
  "detector": "onnx_cpu", "device": "cpu",
  "lifetime_total": 51233, "suspects": 4,
  "suspect_detail": {"born_downstream": 2, "lost_upstream_near_line": 1,
                     "backward_crossings": 0, "crossed_unconfirmed": 1},
  "bus_connected": true, "store_backlog": 0, "store_dropped": 0,
  "preview_url": "http://127.0.0.1:8081/preview.mjpg",
  "crates": 12, "default_target": 400, "max_target": 2000,
  "ts": 1760000123.4
}
```

`state` is one of `IDLE`, `READY`, `RUNNING`, `PAUSED`, `CLEARING`, `FAULT`, or `OFFLINE`.

`OFFLINE` is the last-will message: the broker publishes it if the vision service dies, so
the UI shows "vision service not running" instead of a stale count that looks live.

## `counter/cmd/<command>` — API → vision, QoS 1

Every command may carry `request_id` (echoed in the ack) and `source` (`ui`, `api`, …).

| Topic | Payload | Notes |
|---|---|---|
| `cmd/select_batch` | `{"crate": 3, "target": 400}` | Only in `IDLE`/`READY`. Replacing a `READY` batch aborts the old one. |
| `cmd/cancel_batch` | `{}` | Only in `READY`/`PAUSED`; marks the batch `aborted`. |
| `cmd/start` | `{}` | Needs a selected batch, no active stop button, and live camera frames. Refused in `FAULT`. |
| `cmd/stop` | `{}` | Always accepted. Drops the permit; counting continues while the belt clears. |
| `cmd/finish_batch` | `{}` | Only in `CLEARING`: the operator confirms the belt is clear before `clear_time_s` elapses. |
| `cmd/reset_fault` | `{}` | Only in `FAULT`, and only once frames are arriving again. Does **not** restart the conveyor. |

## `counter/ack` — QoS 1

```json
{"request_id": "…", "command": "start", "ok": false,
 "reason": "select a crate and target first", "ts": 1760000123.4}
```

Rejections are acked too, with the controller's own wording, so the UI can show why a
start did nothing instead of appearing to ignore the press.

## `counter/event` — QoS 1, not retained

One message per controller event; the same events are written to the `events` table.

`state_changed`, `batch_selected`, `batch_recovered`, `count`, `target_reached`,
`batch_completed`, `batch_aborted`, `fault`, `fault_cleared`, `stop_button`,
`command_rejected`, `unassigned_counts`.

```json
{"event": "fault", "state": "FAULT", "batch_id": "4f1c…",
 "reason": "frames_lost:7", "reasons": ["frames_lost:7"], "ts": 1760000130.1}
```

`unassigned_counts` means objects crossed the line with no batch open — nobody knows which
crate they went to. It should stay at zero; if it does not, the gate was opened before a
crate was selected.

## Preview stream (not MQTT)

The vision service serves the annotated frames itself, because it is the only process that
has them: `http://<preview.host>:<preview.port>/preview.mjpg` (also `/snapshot.jpg`).
Rate-limited to `preview.fps`; a slow client never delays the camera loop.
