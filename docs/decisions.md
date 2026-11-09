# Decisions log

| Date | Decision | Why / notes |
|---|---|---|
| 2026-09-26 | Hardware: Radxa Dragon Q6A (8 GB) + NPU, Hikrobot MV-CS016-10GC GigE, PoE switch, sealed enclosure + controlled lighting | Real-time detection needs an accelerator (Pi 5 CPU was too slow for YOLOv8) |
| 2026-09-26 | Services: vision (owns counting + conveyor interlock), API, React kiosk, SQLite, local MQTT | The count and the stop must not depend on a browser being connected (lesson from the Pi project) |
| 2026-09-26 | Backend framework: **FastAPI** (not Flask) | Native async WebSocket; reuses the Pi project's backend patterns |
| 2026-09-26 | Develop on PC in Docker, deploy arm64 images to the board | PC ↔ board seams: frame source, detector, IO backend selected in `site.yaml` |
| 2026-09-26 | Conveyor interlock: **one relay output** in the DOL contactor control circuit, energized = run permitted | De-energize-to-stop: crash / power loss / reset all stop the conveyor |
| 2026-09-26 | Three more GPIO: start push button (NO), stop push button (NC, open = stop), alarm light (ON when stopped, flashing on fault) | |
| 2026-09-26 | Start only works with a crate + target selected, on a button **press** (edge), never on a held button; no automatic restart after any stop or fault | Running without a batch would move coconuts uncounted |
| 2026-09-26 | Count on line **crossing** (upstream → downstream) with min detections per track; report "suspect" events | Fixes the Pi project's double-count risk; makes miscounts visible |
| 2026-09-26 | Tracker uses a belt-velocity prior | Without it, count collapsed at 15 fps (240 → 137 on the test video); with it 234 |
| 2026-09-26 | `safety.min_fps` = 20 for now: each object must be seen in ≥ ~8 frames | 250-coconut video: 30 fps stable, 15 fps −2.5 %, 10 fps unusable. Recalculate for the real camera view and belt speed |
| 2026-11-04 | Ground truth of `250_coconuts.mp4` = **240** (confirmed by eye) | Offline count 240, 0 suspects |
| 2026-11-04 | Dev-only `replay` detector backend: replays `count_eval --save-dets` detections at the camera rate; config refuses it unless video_file + sim IO | A laptop CPU (~5–10 fps for YOLOv8s) drops most frames in real-time mode and counts almost nothing; replay lets the service/API/UI run like the board on any PC |
| 2026-11-04 | Crate presets are owned by the API in its own SQLite file (`data/api.db`); the target that counts is the one sent with `select_batch` and stored on the batch | Keeps the vision DB single-writer; changing a preset never alters a running batch |
| 2026-11-04 | API reuses `vision.config` and `vision.store.db` (pure Python) instead of copies | One definition of the config, schema and CSV format |
| 2026-11-04 | The API serves the built UI (`services/ui/dist`) at `/` | One process on the PC; whether the board uses this or nginx is decided at checkpoint 9 |
| 2026-11-04 | UI shows a value as live only if a non-retained state arrived in the last 2.5 s; otherwise red banner, greyed "last known" numbers, START disabled | No stale count presented as live |
| 2026-11-04 | Shutdown / reboot / restart from the UI: off by default (`api.allow_system_control`); refused while RUNNING or CLEARING; on the board via a sudoers rule for exactly those commands | |

## Open
- Board OS image version (checkpoint 8).
- Model size YOLOv8s vs YOLOv8n and 640×480 input (from NPU benchmarks, checkpoint 8).
- External watchdog hardware for the relay (timer relay fed by a heartbeat pulse, or a safety relay) (checkpoint 10).
- Manual count corrections from the UI (table exists; who may post one, PIN or not) (open).

## Safety note on the stop button
The GPIO stop input makes the software aware of a stop request and pauses the batch.
It is **not** a safety function. The DOL circuit must keep a **hardwired** stop / E-stop in series with the
contactor coil, independent of the board, with the software stop contact wired as a second contact
on the same push button (or a separate button).
