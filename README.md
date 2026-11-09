# Conveyor Counter

Vision-based conveyor counting with a fail-safe conveyor interlock.
First product: coconuts into 12 de-husking crates. Everything product-specific lives in
`config/products/<product>/`; nothing under `services/` mentions coconuts.

Target: Radxa Dragon Q6A + Hikrobot MV-CS016-10GC. Developed and tested on a PC first.

## Layout

```
config/site.yaml                       plant settings: crates, camera, detector backend, GPIO, safety timings
config/products/coconut/profile.yaml   product settings: model file, thresholds, counting line
config/products/coconut/models/        model files (not in git: copy best.onnx here)
services/vision/                       vision service: camera -> detector -> tracker -> counter -> controller -> relay
services/vision/vision/main.py         the service entry point
services/vision/vision/store/db.py     SQLite (WAL): batches, counts, events, corrections
services/vision/vision/bus/mqtt.py     state/event publish + command subscribe (docs/mqtt-contract.md)
services/vision/vision/preview/mjpeg.py  annotated MJPEG preview served by the vision service
services/api/                          FastAPI backend for the kiosk: REST + WebSocket, crate presets, reports
services/ui/                           React kiosk UI (Vite). `npm run build` -> dist/, served by the API at /
services/mqtt/mosquitto.conf           local broker config
docker-compose.dev.yml                 PC development: the broker only
tools/eval/count_eval.py               offline count check on recorded video (same code as the service)
tools/board/gpio_check.py              wiring check on the board
data/                                  videos, evaluation output, database (not in git)
```

## Checkpoints

| # | What | Where | Pass criteria | Status |
|---|---|---|---|---|
| 1 | Repo, config loader + validation | PC | `pytest` green; `python -m vision.config` prints the config | ✅ |
| 2 | Vision core: video source, ONNX CPU detector, tracker, crossing counter, `count_eval` | PC | unit tests green; `count_eval` on `250_coconuts.mp4` within tolerance | ✅ code / ⚠️ see note |
| 3 | Conveyor controller (state machine) + IO layer (sim + gpiod) + IO thread | PC | `pytest` green (39 tests) | ✅ |
| 4 | Vision service main loop, SQLite batch persistence + power-loss recovery, MQTT, preview stream | PC + Docker (mosquitto) | end-to-end run on the video with the sim IO; kill -9 mid-batch recovers | ✅ |
| 5 | FastAPI backend (REST + WebSocket) | PC | API tests (14); live counts over WebSocket | ✅ |
| 6 | React kiosk UI | PC | full batch flow in a browser | ✅ |
| 7 | Whole stack in Docker Compose | PC | one command up; end-to-end demo | next |
| 8 | Board bring-up: OS, MVS camera, NPU model conversion + benchmark; `HikMvsSource`, NPU detector | Board | fps numbers; NPU count = PC count on test videos | in progress: camera source + `camera_check` ready, NPU next |
| 9 | arm64 images, board compose, kiosk systemd unit | Board | stack runs from images after reboot | |
| 10 | Real relay/buttons/lamp + external watchdog + fault-injection tests | Board + panel | every fault test stops the conveyor and shows why | |
| 11 | Shadow mode at the plant, then enable the stop | Plant | count error within target over 1–2 weeks | |

Board: GitHub → Q6A → camera test, step by step: **[docs/board-setup.md](docs/board-setup.md)** (config `config/site.board.yaml`).

## PC setup (Windows or Linux, Python 3.10+)

```bash
cd services/vision
python -m venv .venv
# Windows: .venv\Scripts\activate      Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
```

Copy the model and a test video:
```
config/products/coconut/models/best.onnx     <- Coconut_Counting_V2/models/train2/weights/best.onnx
data/videos/250_coconuts.mp4                 <- Coconut_Counting_V2/videos/250_coconuts.mp4
```
Ground truth of `250_coconuts.mp4` is **240** (confirmed by eye), despite the file name.

### Checkpoint 1
```bash
cd services/vision
python -m vision.config          # prints resolved config, says whether the model file is found
python -m pytest -q
```

### Checkpoint 2
From the repo root:
```bash
# full run: count, annotated video, per-count CSV, and a detection cache for fast re-tuning
python tools/eval/count_eval.py --expected 240 --out data/eval/250_annotated.mp4 \
    --events-csv data/eval/250_events.csv --save-dets data/eval/250_dets.npz

# re-tune tracking/counting settings in seconds (no model run)
python tools/eval/count_eval.py --expected 240 --load-dets data/eval/250_dets.npz

# simulate a slower board: every 2nd frame (15 fps from a 30 fps video)
python tools/eval/count_eval.py --expected 240 --stride 2 --save-dets data/eval/250_dets_s2.npz
```
Watch `data/eval/250_annotated.mp4`: green box = counted, orange = tracked but not yet counted,
grey = raw detection, red band = counting line and its dead band.
The `suspects` line lists events that may hide a miscount (object first seen past the line, track lost
near the line, backward crossing, crossed but never confirmed). A good result has count = truth
and suspects near zero.

### Checkpoint 3
```bash
cd services/vision
python -m pytest -q tests/test_controller.py -v
```

### Live service on a PC: `replay` vs `onnx_cpu`

`count_eval` processes **every** frame, however long that takes. The live service paces the video
like a real camera and **drops** frames it is too slow for. A laptop CPU runs YOLOv8s at roughly
5–10 fps, so the tracker sees each coconut only once or twice and almost nothing gets counted (all
boxes stay orange). That is the correct behaviour for a too-slow processor, and on the conveyor the
`min_fps` check would fault and stop it.

For PC development `config/site.yaml` therefore uses `detector.backend: replay`: the service replays
the detections cached by `count_eval --save-dets` (`data/eval/250_dets.npz`) at the full 30 fps, so
the service, API and UI behave like the board will. It is refused unless the source is a video file
and the IO is simulated. To run the real model on the CPU instead, set `backend: onnx_cpu` and
`realtime: false` (slow motion, every frame) and lower `safety.min_fps` for that test only.
Regenerate the cache after retraining the model (`count_eval ... --save-dets data/eval/250_dets.npz`).

### Checkpoint 4 — run the service
```bash
docker compose -f docker-compose.dev.yml up -d        # mosquitto on localhost:1883
cd services/vision
python -m vision.main                                 # add --stride 2 to simulate a slower board
```

Then, in another shell:
```bash
# watch everything the UI will see
mosquitto_sub -t 'counter/#' -v

# run a batch: 12 crates, 400 per crate by default
mosquitto_pub -t counter/cmd/select_batch -m '{"request_id":"1","crate":3,"target":50}'
mosquitto_pub -t counter/cmd/start        -m '{"request_id":"2"}'
mosquitto_pub -t counter/cmd/stop         -m '{"request_id":"3"}'
mosquitto_pub -t counter/cmd/reset_fault  -m '{"request_id":"4"}'
```

Annotated live view: <http://127.0.0.1:8081/> (`/snapshot.jpg` for a single frame).

Inspect the database and export a production day:
```bash
cd services/vision
python -m vision.store.db                        # today's summary + any open batch
python -m vision.store.db --day 2026-10-18 --csv -
```

Power-loss test (the point of checkpoint 4):
```bash
# with a batch part-counted:
kill -9 <pid of vision.main>
python -m vision.main
# the log says: recovered batch <id>: crate N, X/Y counted, state PAUSED - marked UNCERTAIN
```
The batch comes back with its count, is flagged `uncertain` for review, and the conveyor
stays stopped until an operator presses START. Nothing restarts by itself.

### Checkpoints 5–6 — API and kiosk UI (PowerShell)

One-time setup (separate venv for the API; it needs no OpenCV or ONNX Runtime):
```powershell
cd C:\Anjana\Coconut_Counting_V2\conveyor-counter\services\api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m pytest -q                       # 14 tests

cd ..\ui
npm install
npm run build                             # -> services/ui/dist, served by the API at /
```

Run (four terminals, from the repo root):
```powershell
docker compose -f docker-compose.dev.yml up -d                                  # 1. broker
cd services\vision; .\.venv\Scripts\Activate.ps1; python -m vision.main        # 2. vision (replay)
cd services\api;    .\.venv\Scripts\Activate.ps1; python -m api.main           # 3. API on :8000
```
Open <http://localhost:8000>. For UI work with hot reload use `cd services\ui; npm run dev` instead
and open <http://localhost:5173> (it forwards `/api` to :8000).

| Screen | What it does |
|---|---|
| Production | active batch (state, count / target, remaining, overfill, rate, time to target), START / STOP, Finish / Cancel / Reset fault, camera view, 12 crate tiles. Tap a tile: keypad to change its target, "Save & select crate". "Set all targets" sets every crate. |
| Reports | one production day at a time: totals, per crate, every batch with review flags; Copy CSV to USB (falls back to `data/exports` on a PC); Download CSV |
| System | live diagnostics (permit, camera, fps, inference, DB queue, suspects), event log, restart / reboot / shut down (off unless `api.allow_system_control`) |

What the UI does when something is down:
- backend, broker or vision service unreachable, or no fresh state for 2.5 s: red banner, numbers
  greyed and marked "last known", START disabled. It never shows an old count as live.
- vision service killed: the broker publishes its last will, so the UI shows OFFLINE at once.
- every refused command shows the vision service's own reason ("select a crate and target first").
- a crate preset is only a default; a running batch keeps the target it was started with.

API: `GET /api/info`, `GET /api/state`, `GET /api/crates`, `PUT /api/presets/{crate}`,
`PUT /api/presets` (all), `POST /api/batch/select`, `POST /api/command/{start|stop|finish_batch|cancel_batch|reset_fault}`,
`GET /api/reports/day?day=`, `GET /api/reports/day.csv?day=`, `POST /api/reports/export`, `GET /api/usb`,
`POST /api/system/{restart|reboot|shutdown}`, WebSocket `/api/ws`. Interactive docs: <http://localhost:8000/docs>.

## Field IO (conveyor interlock)

| Signal | Direction | Meaning |
|---|---|---|
| `run_permit` | output → relay | Relay contact in series with the DOL contactor coil circuit. Energized = conveyor allowed to run. |
| `alarm_light` | output → lamp | ON while the conveyor is not permitted to run; flashing = fault. |
| `start_button` | input | NO push button. A press (not a hold) starts/resumes a selected batch. |
| `stop_button` | input | NC push button, wired so pressed OR broken wire = stop. |

Fail-safe behaviour:
- the relay is only energized in RUNNING;
- any fault, stop, crash, power loss or reset drops it;
- after any stop, only an operator START runs the conveyor again.

## What stops the conveyor

| Condition | Detected by | Result |
|---|---|---|
| Target reached | counter | permit OFF, keeps counting until the belt is clear, final count saved |
| STOP button / UI stop | IO thread | permit OFF, counting continues, START resumes |
| No frames for `frame_timeout_ms` | IO thread (independent of the camera loop) | FAULT |
| Camera frame-ID gap > `max_frame_gap` | vision loop | FAULT, `frames_lost` recorded on the batch |
| Processing below `min_fps` | IO thread | FAULT |
| Database writes failing | store writer → service | FAULT if `safety.fault_on_db_error` |
| Broker down | service | shown in `state`; FAULT only if `safety.fault_on_bus_loss` |
| IO thread stops cycling | vision loop | FAULT |
| Process killed / power loss / board reset | nothing holds the relay | conveyor stops |

Every batch that saw a fault or a restart is stored with `uncertain = 1` and appears in the
report as needing review, so a questionable count is never presented as a clean one.
