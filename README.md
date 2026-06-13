# nightspot

An interactive photo-spot installation. A Sony a7 III in a second-story
window points at a sidewalk spot under a streetlight. Visitors scan a QR code
at the spot and move through a server-enforced, locked flow on their phone:

```
start -> capture -> review -> path -> rec -> deposited -> gifted -> done
```

Python 3.9 / FastAPI / Jinja2 / SQLite (stdlib) / uvicorn. The camera is
driven over USB with `gphoto2` subprocess calls; set `MOCK_CAMERA=1` to run
the whole thing with no camera attached (Pillow generates placeholder JPEGs
for captures and the live preview).

## Dev run (no camera)

```bash
pip install -r requirements.txt
MOCK_CAMERA=1 python3 -m uvicorn app:app --port 8000
```

Open <http://localhost:8000>. The landing page footer shows the version
string (currently `v1`). Bump `VERSION` in `app.py` on any change.

## Run the test suite

```bash
MOCK_CAMERA=1 python3 -m pytest test_app.py -q
# or, with no pytest installed:
MOCK_CAMERA=1 python3 test_app.py
```

## Environment variables

| Var            | Default | Meaning                                            |
|----------------|---------|----------------------------------------------------|
| `MOCK_CAMERA`  | unset   | `1` = no camera; mock JPEGs for capture & preview. |
| `NIGHT_START`  | `20`    | Hour (local) night begins. Landing flips to "Start".|
| `NIGHT_END`    | `6`     | Hour (local) night ends.                           |
| `MAX_TAKES`    | `5`     | Retake ceiling; Sad button disappears at the cap.  |
| `PREVIEW_TTL`  | `2`     | Seconds between real liveview pulls (server-side). |
| `CAMERA_ROTATE`| `0`     | Rotate frames N° clockwise (0/90/180/270) — fixes a sideways preview. |
| `PREVIEW_MAX`  | `800`   | Max px for liveview frames served to phones (downscaled, keeps it light). |
| `TIP_URL`      | empty   | Shelved tip door; restore path is in `/exit`.      |
| `ADMIN_KEY`    | `nightspot` | Key for the `/admin` dashboard. **Change this.** |
| `SMTP_HOST`    | empty   | Mail server; set (with the rest) to enable email alerts. |
| `SMTP_PORT`    | `587`   | `587` STARTTLS, or `465` SSL.                      |
| `SMTP_USER`    | empty   | SMTP login / sending address.                      |
| `SMTP_PASS`    | empty   | SMTP password or app-password.                     |
| `NOTIFY_FROM`  | `SMTP_USER` | From address on the alert emails.              |
| `NOTIFY_TO`    | empty   | Where alerts go (e.g. evan@just-in-time.co).       |

## Email alerts (someone used the service)

When SMTP is configured, the app emails `NOTIFY_TO` on every real event:

- a **deposit** (a recommendation / memory / fear was left),
- a **question** for the window,
- a new **signup**.

Sends happen on a background thread and swallow all errors, so a slow or
broken mail server can never delay or break a visitor's flow. With SMTP
**unconfigured it's a silent no-op** — dev runs and the test suite never touch
the network.

**Easiest setup — a drop-in config file.** Copy `nightspot.env.example` to
`nightspot.env` (gitignored, so secrets never get committed) and fill it in;
it's loaded automatically on startup. Real env vars / systemd `Environment=`
lines still take precedence.

```bash
cp nightspot.env.example nightspot.env   # then edit it and paste the App Password
```

Verify it actually sends (after filling in the password):

```bash
.venv/bin/python -c "import logging; logging.basicConfig(level=logging.INFO); import app, notify; notify._deliver('nightspot test', 'it works')"
```

For Gmail use a 16-character **App Password**, not your account password
(Google Account → Security → 2-Step Verification → App passwords). Or, in a
systemd unit:

```ini
Environment=SMTP_HOST=smtp.gmail.com
Environment=SMTP_PORT=587
Environment=SMTP_USER=nightspot.indy@gmail.com
Environment=SMTP_PASS=your-16-char-app-password
Environment=NOTIFY_TO=nightspot.indy@gmail.com
```

## Admin dashboard

A web view of every submission lives at **`/admin?key=<ADMIN_KEY>`** (default
key `nightspot` — override with the `ADMIN_KEY` env var before going live).
It lists, newest first:

- **Subscribers** — name, phone, the deposit set aside for them, sent status.
- **Deposits** — every recommendation / memory / fear, text or voice (inline
  audio), with the photo thumbnail and how many times it's been given.
- **Questions for the window.**

Photos and voice in the dashboard are served by `/admin/photo/{id}` and
`/admin/voice/{id}`, also key-gated. Keep the URL (with its key) private.

## "A text a day from a stranger" (signup)

The exit menu's **Get one** door (`/subscribe`) takes a name + phone and sets
aside one prior stranger's deposit (least-circulated first) for that person,
recorded in the `subscribers` table.

**Sending the actual text is not wired yet** — it needs an SMS/MMS provider.
The send hook is marked `TODO(sms)` in `app.py`'s `api_subscribe`: name,
phone, and the assigned `gift_id` are all stored, so a daily job can later
pull un-sent rows (`SELECT * FROM subscribers WHERE sent=0`), MMS the deposit's
photo + text via Twilio (or similar), and flip `sent=1`. HTTPS via the
Cloudflare Tunnel is already required for the microphone, so the public URL is
in place.

Read the signups:

```bash
sqlite3 -header -column data/nightspot.db "SELECT id,name,phone,category,gift_id,datetime(created_at,'unixepoch','localtime') AS at,sent FROM subscribers ORDER BY id;"
```

## Sony a7 III prep (for the real rig)

On the camera:

- **MENU → Setup → USB Connection = `PC Remote`**
- **MENU → Setup → USB Power Supply = `On`**
- **MENU → Setup → Power Save / Auto Power Off = `Off`** (it must never sleep)
- **MENU → Network → Ctrl w/ Smartphone = `Off`** (frees the USB control path)
- **MENU → Setup → PC Remote settings → Still Img. Save Dest. = `PC+Camera`**
  (so `--keep` actually leaves an archive copy on the card)
- Lens: set the **aperture ring to f/4**, flick the **AF/MF switch to MF**, and
  pre-focus on the spot once (then leave it; MF holds focus across captures).

Confirm `gphoto2 --auto-detect` lists the camera before starting the service.

## Raspberry Pi 5 install + systemd

```bash
sudo apt update && sudo apt install -y python3 python3-venv gphoto2 libgphoto2-dev
cd /home/pi
git clone <this-repo> nightspot && cd nightspot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

`/etc/systemd/system/nightspot.service`:

```ini
[Unit]
Description=nightspot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/nightspot
Environment=NIGHT_START=20
Environment=NIGHT_END=6
# Remove MOCK_CAMERA on the real rig; keep it to test without the a7 III.
# Environment=MOCK_CAMERA=1
ExecStart=/home/pi/nightspot/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nightspot
journalctl -u nightspot -f
```

## Cloudflare Tunnel (HTTPS — required for the microphone)

The voice deposit uses `MediaRecorder` / `getUserMedia`, which browsers only
expose over **HTTPS**. Put the app behind a Cloudflare Tunnel so phones reach
it over TLS:

```bash
cloudflared tunnel login
cloudflared tunnel create nightspot
cloudflared tunnel route dns nightspot spot.example.com
cloudflared tunnel --url http://localhost:8000 run nightspot
```

Encode `https://spot.example.com/` into the QR code at the spot.

## SQLite one-liners

Database lives at `data/nightspot.db`.

Seed the **rec** door bank:

```bash
sqlite3 data/nightspot.db "INSERT INTO memories (session_id,category,kind,body,created_at,times_given) VALUES ('seed','rec','text','The taco truck two blocks north, after 11pm.',strftime('%s','now'),0);"
```

Seed the **fear** door bank:

```bash
sqlite3 data/nightspot.db "INSERT INTO memories (session_id,category,kind,body,created_at,times_given) VALUES ('seed','fear','text','That the light will burn out and no one will replace it.',strftime('%s','now'),0);"
```

(The **memory** door is seeded automatically on first run with the yellow
rose.)

Read the memory bank:

```bash
sqlite3 -header -column data/nightspot.db "SELECT id,session_id,category,kind,times_given,substr(body,1,50) AS body FROM memories ORDER BY id;"
```

Read the questions left for the window:

```bash
sqlite3 -header -column data/nightspot.db "SELECT id,session_id,datetime(created_at,'unixepoch','localtime') AS at,body FROM questions ORDER BY id;"
```

## Versioning

There is one version string, `VERSION` in `app.py`, shown in the landing
footer. Bump it on every change so there is never ambiguity about which build
is running. Commit when working.
