# 1. OS & Environment Verification:

To ensure compatibility with ARM64 packages.

- Check OS version:
```Bash
cat /etc/os-release
```

This confirmed the board is running Ubuntu 24.04 LTS (Noble Numbat) on an ARM64 (aarch64) architecture.

- Check Kernel Version:
```Bash
uname -a
```

This verified the running Linux kernel build

# 2. Hardware Interfacing & Network Configuration

Once the camera is connected to the board via PoE switch, we needed to bridge the network subnets so the SBC could talk to the camera.

- Identify the Network Interface Name:

```Bash
sudo ip a
```

Unlike standard eth0 names, modern Ubuntu assigns predictable interface names. The wired interface was named <b>enp1s0</b>.

- Install Nmap for Network Discovery:

```Bash
sudo apt update && sudo apt install -y nmap
```

Installed network diagnostic utilities to scan local subnets.

- Assign a Temporary Static IP & Bring Up the Interface:

```Bash
sudo ip addr add 192.168.0.100/24 dev enp1s0
sudo ip link set enp1s0 up
```

Hikrobot GigE cameras default to the 192.168.0.x subnet. Giving the board an IP on that same subnet (192.168.0.100) established the initial layer-3 communication path.

- Verify Physical Link Speed:

```Bash
ethtool enp1s0
```

Checked that the PoE switch and camera successfully negotiated a 1000Mb/s Full Duplex Gigabit link.

# 3. Hikrobot MVS SDK Installation

Once the network layer was established, we installed the official driver and development kit.

- Download and Extract the ARM64 SDK Package:

Downloaded MVS-5.1.0_Linux_aarch64_20260909.tar.gz and extracted it.

- Run the SDK Setup Script:

```Bash
cd /home/radxa/Downloads/MVS-5.1.0_Linux_aarch64_20260909/MVS-5.1.0_aarch64_20260909
sudo ./setup.sh
```

This script automatically creates udev hardware rules for vendor ID 2bdf, adjusts USB/socket buffer limits, configures reverse-path filtering (rp_filter), and places core utilities into /opt/MVS/bin.

# 4. Camera Discovery and IP Configuration

- Launch the IP Configuration Tool:

```Bash
cd /opt/MVS/bin
./Ip_Configurator
```

This GUI tool broadcasts raw Ethernet packets to discover cameras across subnets regardless of mismatch.

- Update Camera IP:

In the tool window, I saw the camera stuck on a Link-Local address (169.254.242.109) marked as <span style="color:orange;">Unreachable</span>. I double-clicked the camera entry and assigned it a permanent static IP of 192.168.0.64 with subnet mask 255.255.255.0 to match the board. Once applied, the status changed to <span style="color:green;">Free</span> and ping testing succeeded (0.8ms latency).

# 5. Launching MVS Client & Tuning Image Quality

- Launch the MVS GUI:

```Bash
/opt/MVS/bin/MVS.sh
```

Opened the official Hikrobot interface where the MV-CS016-10GC (Serial: DA9906041) appeared.

- Fixing the Dark Image / Exposure:

The video strewam was extremely dark with only the light source visible, even in lit rooms. Corrected by:

<ul>
<li>Reduce the lens apature to the lowest (16 to 2.4)</li>
<li>Acquisition Control > Increase ExposureTime (From 5000 to 10000)</li>
<li>Analog Grain > Set to Auto (Continuous)</li>
</ul>

# 6. Making the Static IP Persistent (Netplan)

Because temporary `ip addr add` commands are wiped out on reboot, Netplan was configured to make the static IP permanent.

- Edit the Netplan Configuration File:


```Bash
sudo nano /etc/netplan/01-netcfg.yaml
```

- Configure with NetworkManager Backend:

Following structure was added to ensure it integrates cleanly with the desktop environment's network manager:

```Bash
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    enp1s0:
      dhcp4: no
      addresses:
        - 192.168.0.100/24
```

- Secure File Permissions and Apply:

```Bash
sudo chmod 600 /etc/netplan/01-netcfg.yaml
sudo netplan apply
```

Netplan strictly requires restrictive permissions (600) for security. Applying this ensures that every time the Radxa board is restarted, <b>enp1s0</b> automatically boots up with 192.168.0.100, keeping your camera instantly ready on startup.

---
# App setup

## One time (powershell)

```Bash
cd services\vision
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt 
deactivate

cd ..\..
cd services\api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt 
deactivate 

cd ..\..
cd services\ui 
npm install 
npm run build
cd ..\..
```

## each time (separate terminals)
```Bash
docker compose -f docker-compose.dev.yml up -d

cd services\vision
.\.venv\Scripts\Activate.ps1
python -m vision.main

cd services\api    
.\.venv\Scripts\Activate.ps1
python -m api.main
```