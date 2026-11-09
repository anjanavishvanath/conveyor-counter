# Board setup: GitHub → Radxa Dragon Q6A → camera test

**Assumes:** Radxa Ubuntu 24.04 LTS (Noble) aarch64 image, user `radxa`, Hikrobot MVS 5.1.0 already
installed in `/opt/MVS` (SETUP.md), camera at `192.168.0.64` on `enp1s0` (`192.168.0.100`), and
internet on the board through Wi-Fi (the Ethernet port is the camera segment).
**Stage:** camera bring-up (checkpoint 8). CPU detector, simulated IO: no relay is wired, nothing
here can move the conveyor.

---

## 1. PC: push the repo to GitHub (PowerShell)

One time: install Git for Windows, then
```powershell
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

Create an **empty private** repository on github.com named `conveyor-counter`
(no README, no .gitignore, no licence: the repo already has them).

```powershell
cd C:\Anjana\Coconut_Counting_V2\conveyor-counter
git init -b main
git add -A
git status                                     # read the list before committing

# must print nothing: no models, videos, databases, venvs or node_modules
git ls-files | Select-String -Pattern '\.onnx$|\.pt$|\.mp4$|\.db|\.npz$|node_modules|\.venv|/dist/'

git commit -m "Conveyor counter: vision service, API, kiosk UI, board config"
git remote add origin https://github.com/<your-user>/conveyor-counter.git
git push -u origin main                        # a browser window asks you to sign in to GitHub
```

Not in git, on purpose: `best.onnx` (45 MB, copied in step 4), everything under `data/`
(databases, exports, videos), `.venv`, `node_modules`, `services/ui/dist`.

Later changes: `git add -A; git commit -m "..."; git push`.

## 2. Board: system packages

```bash
sudo apt update
sudo apt install -y git python3-venv python3-dev build-essential \
                    mosquitto mosquitto-clients nodejs npm openssh-server
python3 --version        # 3.12.x on Ubuntu 24.04
node --version           # 18.x from Ubuntu is enough for the UI build
hostname -I              # note the Wi-Fi address: <board-ip> below
```

## 3. Board: read-only GitHub access (deploy key) and clone

A deploy key gives this board read access to this one repository only.

```bash
ssh-keygen -t ed25519 -C "q6a-coconut" -f ~/.ssh/github_counter -N ""
cat ~/.ssh/github_counter.pub
```
On GitHub: repo → **Settings → Deploy keys → Add deploy key** → paste, leave
"Allow write access" **unticked**.

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_counter
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh -T git@github.com      # answer "yes" once; expect "Hi <your-user>/conveyor-counter! You've successfully authenticated"

git clone git@github.com:<your-user>/conveyor-counter.git ~/conveyor-counter
```

## 4. PC → board: the model file

From PowerShell on the PC (Windows 10/11 has `scp` built in):
```powershell
scp C:\Anjana\Coconut_Counting_V2\conveyor-counter\config\products\coconut\models\best.onnx `
    radxa@<board-ip>:/home/radxa/conveyor-counter/config/products/coconut/models/
```

## 5. Board: Python environments and tests

```bash
cd ~/conveyor-counter/services/vision
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt pytest     # not requirements-dev: its GUI OpenCV clashes with the headless one
.venv/bin/python -m pytest -q

cd ~/conveyor-counter/services/api
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## 6. Board: build the UI

```bash
cd ~/conveyor-counter/services/ui
npm ci
npm run build            # -> services/ui/dist, served by the API
```

## 7. Board: MQTT broker

```bash
sudo cp ~/conveyor-counter/deploy/board/mosquitto-counter.conf /etc/mosquitto/conf.d/counter.conf
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto
systemctl status mosquitto --no-pager | head -5         # active (running)
```

## 8. Board: camera network check

```bash
ping -c 3 192.168.0.64                                   # camera answers
sysctl net.core.rmem_max net.ipv4.conf.all.rp_filter     # MVS setup.sh raised rmem and relaxed rp_filter
ip link show enp1s0 | grep -o 'mtu [0-9]*'
```
Jumbo frames cut CPU load and lost packets. Only if the PoE switch supports them
(UNVERIFIED for the Q6A's NIC): `sudo ip link set enp1s0 mtu 9000` to test, then make it
permanent by adding `mtu: 9000` under `enp1s0:` in `/etc/netplan/01-netcfg.yaml` and
`sudo netplan apply`. Without jumbo frames set `packet_size: 1500` in `config/site.board.yaml`
if the check below reports incomplete frames.

## 9. Board: camera check (no detector, no broker, no UI)

**Close the MVS client first**: the camera opens exclusively.

```bash
cd ~/conveyor-counter
export COUNTER_SITE_CONFIG=$HOME/conveyor-counter/config/site.board.yaml

# Where the SDK put its Python binding and library. If they differ from
# sdk_python_path / sdk_lib in config/site.board.yaml, edit those two lines.
find /opt/MVS -name MvCameraControl_class.py
find /opt/MVS -name libMvCameraControl.so

services/vision/.venv/bin/python tools/board/camera_check.py --list
services/vision/.venv/bin/python tools/board/camera_check.py --seconds 60 --snapshot data/cam.jpg
```
Pass: ~30 fps, 0 lost frames, 0 incomplete frames over 60 s. Then look at `data/cam.jpg`
(on the board, or `scp radxa@<board-ip>:~/conveyor-counter/data/cam.jpg .` on the PC):
sharp, not too dark, and **a red object must look red** (if red and blue are swapped, tell
Claude: the Bayer order needs changing).

## 10. Board: run the stack by hand

Three terminals (or tmux), each starting with
`export COUNTER_SITE_CONFIG=$HOME/conveyor-counter/config/site.board.yaml`:

```bash
# terminal 1: vision
cd ~/conveyor-counter/services/vision && .venv/bin/python -m vision.main
# terminal 2: API + UI
cd ~/conveyor-counter/services/api && .venv/bin/python -m api.main
# terminal 3: watch the bus (optional)
mosquitto_sub -t 'counter/event' -t 'counter/ack' -v
```
Open `http://<board-ip>:8000` on the PC, or `http://localhost:8000` on the board.

What to expect at this stage:
- **System tab:** Detector `onnx_cpu on cpu`, a few fps, inference in the hundreds of ms. That is the
  CPU; the NPU backend (rest of checkpoint 8) is what brings it to 30 fps.
- **Camera view:** detections on whatever is under the camera. The counting line, direction and ROI
  in `config/products/coconut/profile.yaml` were set for the webcam video (belt moving **up** the
  image). Match them to how this camera is mounted (`counting.axis`, `direction`, `line_position`).
- A test batch works with the simulated IO (`min_fps: 3` in the board config is a BENCH value).
  Moving coconuts will be missed at CPU speed. Count accuracy is tested once the NPU runs.

## 11. Board: run as services (after step 10 works)

```bash
sudo cp ~/conveyor-counter/deploy/board/counter-vision.service ~/conveyor-counter/deploy/board/counter-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now counter-vision counter-api
systemctl status counter-vision counter-api --no-pager
journalctl -u counter-vision -f          # live log; Ctrl-C to leave
```
The units assume user `radxa` and `/home/radxa/conveyor-counter`; edit them if yours differ.
Stop the hand-started processes from step 10 first (the camera opens exclusively).

## 12. Updating the board later

```bash
cd ~/conveyor-counter
git pull
services/vision/.venv/bin/pip install -r services/vision/requirements.txt    # when requirements changed
services/api/.venv/bin/pip install -r services/api/requirements.txt
(cd services/ui && npm ci && npm run build)                                    # when the UI changed
sudo systemctl restart counter-vision counter-api
```
`data/` (production database, presets, exports) is never touched by `git pull`. Back up
`data/counter.db` before big changes.

## Troubleshooting

| Message | Likely cause |
|---|---|
| `cannot import the MVS Python binding` | `sdk_python_path` / `sdk_lib` wrong: use the `find` output from step 9 |
| `no GigE camera found` | cable / PoE, `enp1s0` address, `ping 192.168.0.64` |
| `open camera ... failed` | MVS client still open, or the camera is on another subnet (Ip_Configurator) |
| incomplete frames / lost frames | packet size vs MTU, jumbo frames, `rmem_max`, cable; another program using the NIC |
| `no frame from camera within 1.0 s` | camera stopped streaming: power, cable, or trigger mode left on by the MVS client |
| UI shows "No live data from the vision service" | vision not running or crashed: terminal 1 / `journalctl -u counter-vision` |
| camera view blank from the PC | `preview.host` must be `0.0.0.0` (board config) for a remote browser |
