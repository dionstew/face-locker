# Face Locker

Offline touchscreen face access manager for a Raspberry Pi 4 (4 GB) and a Pi ribbon camera.
Includes the native Qt app, camera/recognition service, supervised enrollment, access calendar,
battery telemetry, serial connection management, and boot configuration. **External lock-controller
firmware, GPIO solenoid drivers, and solenoid timing logic are not included.**

## Try the app

On a desktop with Python 3.10 or later:

On Raspberry Pi OS, Debian, or Ubuntu, install Qt's xcb cursor dependency first:

```bash
sudo apt update
sudo apt install libxcb-cursor0
```

Then install the Python dependencies and start the demo:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py --demo --windowed
```

Demo mode uses a drawn camera feed, a seeded person named Alex Morgan, and an in-memory
serial peer. It never imports Picamera2 or opens a serial port. All screens work, including
simulated successful access, unknown people, failed liveness, history, and administration.
The demo administrator PIN is `123456`; this PIN is **never** set in hardware mode.
Use the simulated-face selector on the Camera screen to change scenarios.

For a distro installation that already has PyQt5, it can be used instead of PySide6.
The UI prefers PySide6 when both are installed. Do not install both unnecessarily.

### Qt xcb startup errors

If Qt reports `xcb-cursor0 or libxcb-cursor0 is needed` or aborts while loading
the `xcb` plugin, install `libxcb-cursor0` with the command above and retry in
your graphical desktop session. This is an OS library; reinstalling pip packages
does not install it. The app checks xcb's shared-library dependencies before
starting a child service and reports the missing library when the loader exposes it.
Other plugin failures (including display access) can still require Qt diagnostics.

For further diagnostics:

```bash
QT_DEBUG_PLUGINS=1 .venv/bin/python app.py --demo --windowed
```

Look for a `cannot open shared object file` line identifying another missing
library. Run the GUI from the Pi/desktop session, or use a correctly configured
remote desktop/X forwarding connection. `offscreen` is only for automated checks;
it does not display an interactive window.

## Install on the Raspberry Pi

Use a current **64-bit Raspberry Pi OS with a desktop**. Connect and verify the ribbon camera
first, for example with `rpicam-hello`. The touch display must already work with the OS.

```bash
sudo apt update
sudo apt install python3-venv python3-pyqt5 python3-opencv python3-numpy python3-picamera2 python3-serial python3-yaml
cd ~/face-locker
python3 -m venv --system-site-packages .venv
.venv/bin/python -c "import cv2; print(cv2.__version__)"
.venv/bin/python scripts/download_models.py
.venv/bin/python app.py --set-admin-pin
.venv/bin/python app.py --standalone
```

**OpenCV must be 4.8 or newer.** If your OS provides an older OpenCV, upgrade the OS/packages
before proceeding. The camera library comes from apt, not from `pip install picamera2`.
The Pi installation uses system Qt/OpenCV to avoid duplicating the camera stack; do not also
install the desktop requirements file into this environment.

The downloader resolves a single OpenCV Zoo revision, verifies each model against its Git LFS
SHA-256 and size, saves licenses, and records the revision in `models/manifest.json`. For a
repeatable installation, use `--revision FULL_COMMIT_SHA` from that manifest.

Configure `config/settings.yaml`:

- `serial_port`: your external controller, such as `/dev/serial0` or a stable `/dev/serial/by-id/...`.
- `baudrate` and `lock_id`: must match the external controller.
- `timezone`: local calendar timezone, default `Asia/Jakarta`.
- `backlight_path`: optional writable brightness file for the installed display.

For hardware UART, enable the serial hardware and disable the Linux serial console through
`sudo raspi-config`. Add the desktop/service user to `dialout` and `video`, then log out/in.
Use a serial interface with appropriate electrical levels; the application only opens the
configured Linux device. See [the serial protocol](docs/serial-protocol.md) before integration.

Sign into Administration and enroll a person. Enrollment collects five quality-checked samples
from the same face. Rotate slightly between samples. Recognition uses active users only.
Disabling a person takes effect immediately for subsequent recognition sessions; deleting a
person removes their templates while retaining named audit history.

### Observation and unlocking

Hardware mode initially runs in **observation mode**: it recognizes enrolled people and records
attempts, but sends no unlock commands. This is deliberate commissioning behavior.

To enable hardware requests:

1. Calibrate `match_threshold` and `match_margin` with genuine and impostor trials on your camera.
2. Supply and validate a compatible liveness/PAD model; configure `pad_model` and its settings.
   Its exact preprocessing/output contract is in [recognition.md](docs/recognition.md).
3. Test the external controller's protocol, independent activation timeout, sensor reporting,
   duplicate handling, and behavior when the Pi loses power or the serial connection.
4. Set `unlock_enabled: true` and restart the service.

Enabling that flag without a configured PAD model still leaves recognition in observation mode.
No liveness weights or claim of certified spoof resistance are bundled. A normal RGB camera and
face matcher alone do not provide secure liveness detection. Actual Pi latency, optics, PAD
accuracy, and physical lock operation must be measured on the installed hardware.

## Start automatically at boot

The backend starts at system boot; the touchscreen starts in an automatically logged-in desktop
session. **Edit the service files for your actual username, project path, and Python environment**
before copying them. The provided backend example uses user `pi` and `/home/pi/face-locker`.
The UI example assumes the project is `~/face-locker`.

```bash
sudo cp deployment/face-locker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now face-locker.service
mkdir -p ~/.config/systemd/user ~/.config/autostart
cp deployment/face-locker-ui.service ~/.config/systemd/user/
cp deployment/face-locker.desktop ~/.config/autostart/
systemctl --user daemon-reload
```

Enable **desktop autologin** for the same user through `sudo raspi-config` and reboot. The desktop
autostart entry imports the display environment into the user service manager, then starts the
fullscreen UI. You do not need to enable the UI service separately. Configure the desktop's
screen blanking policy as appropriate for the installed display.

Logs and maintenance:

```bash
journalctl -u face-locker.service -f
journalctl --user -u face-locker-ui.service -f
sudo systemctl stop face-locker.service
.venv/bin/python app.py --set-admin-pin
sudo systemctl start face-locker.service
```

Changing the PIN requires stopping the backend so no old admin sessions survive. No PIN is
accepted via command-line arguments or stored as plaintext; the database stores a salted hash.

## Screens and operation

- **Dashboard:** Start recognition, battery voltage/freshness, controller connection, reported
  actuator/door state, and daily counts.
- **Camera:** live preview, positioning/quality guidance, enrollment progress, start/cancel,
  and recognition/controller outcome.
- **Calendar:** select a local date to inspect access attempts and device events. Select an
  access row to see its decision reason and separately recorded controller result.
- **Administration:** touchscreen PIN keypad, enrollment name keyboard, user list, disable,
  enable, delete, and sign out. Sessions expire after 15 minutes; five failed PIN attempts
  trigger a 30-second cooldown.
- **Standby:** cancels scanning/enrollment and stops camera capture while serial monitoring runs.
- **Display off:** enters standby, signs out, shows a black touch-to-wake overlay, and optionally
  sets the configured backlight to zero. The complete first tap is consumed by waking the screen.
  Without a configured writable backlight file, this blanks the content but does not switch off
  the physical panel. Wake support depends on the display leaving its touch device active.

There is no OS shutdown button or access scheduling editor in this version. The calendar is
activity history; enabled users have access at any time. Use an RTC/time synchronization if
accurate offline audit timestamps are needed. Unlock timeouts use a monotonic clock.

## Architecture and local data

```text
Qt UI process ── private Unix socket ── application service
                                        ├─ vision worker → Picamera2 / OpenCV / PAD
                                        ├─ SQLite → users, templates, attempts, commands, events, telemetry
                                        └─ serial thread ⇄ external lock controller
```

The backend owns the camera, recognition models, serial port, and database. The UI polls a
snapshot in a worker thread and receives only the latest in-memory JPEG; inference and serial
reads never run in the GUI thread. Capture is 640×480, inference processes fresh frames without
a backlog, and models remain loaded between sessions. Capture closes in standby.

The state machine is `booting → idle → scanning/enrolling → result → idle`. Authorized
recognition produces `unlocking → accepted → completed/fault`; acceptance is not physical
confirmation. Neither startup nor reconnection sends or replays unlock commands.

State defaults to `~/.local/share/face-locker/`, with a separate `face-locker-demo/` directory for
demo mode. Directories are mode 0700, the socket is 0600, and the service uses umask 0077.
The trust boundary is the Linux account: processes running as that user can access its data.
Biometric templates are **not encrypted at rest**. Protect the device/storage and backups;
raw camera frames and enrollment photos are never saved by the app.

The schema is defined in `face_locker/storage.py`. Face templates are tied to the SFace weight
hash; replacing that model requires re-enrollment. A restart marks unfinished scans as
interrupted and pending commands as unknown. Telemetry is stored at most once per minute,
with a rolling limit of 10,000 samples; access and device audit records persist until you
archive/remove them. Calendar lists are limited to the latest 500 entries per date.

## Checks

```bash
.venv/bin/python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/check_demo.py
```

The unit/integration suite checks access decisions, enrollment, PIN handling, calendar time
boundaries, template deletion, restart recovery, and the real pySerial transport through a
Linux pseudo-terminal. `check_demo.py` starts separate UI/backend processes, exercises the
screens, simulated recognition and serial completion, and saves screenshots in `artifacts/`.
These checks do not replace Pi camera, liveness, battery sensor, or physical lock commissioning.

References: [Picamera2](https://www.raspberrypi.com/documentation/accessories/camera-software.html),
[OpenCV YuNet/SFace](https://docs.opencv.org/4.x/d0/dd4/tutorial_dnn_face.html),
[pySerial](https://pyserial.readthedocs.io/en/latest/pyserial_api.html).
