"""Camera module for nightspot.

Drives a Sony a7 III over USB via gphoto2 subprocess calls. When
MOCK_CAMERA=1 is set, every camera operation is replaced with a
Pillow-generated placeholder JPEG so the whole app runs with no camera
attached.

Two operations:
  - capture(session_id, take): fires the shutter, downloads the frame.
    Serialized with a threading.Lock (one body, one shutter) and
    idempotent per take.
  - preview(): pulls a liveview frame (no shutter), throttled to one real
    pull per PREVIEW_TTL seconds no matter how many clients poll. Acquires
    the capture lock NON-blocking so a preview never delays a capture.
"""
import io
import os
import subprocess
import threading
import time
from typing import Optional

MOCK = os.environ.get("MOCK_CAMERA") == "1"
PREVIEW_TTL = float(os.environ.get("PREVIEW_TTL", "2"))
CAPTURE_TIMEOUT = 30

# When the python-gphoto2 binding is installed (on the Pi: `pip install
# gphoto2`), hold the camera open and pull frames continuously for smooth,
# video-rate liveview instead of spawning a fresh gphoto2 process per frame.
# Falls back to subprocess gphoto2 (or mock) when the binding isn't present, so
# the dev Mac and un-upgraded Pis keep working unchanged.
try:
    import gphoto2 as gp  # type: ignore
    HAVE_GP = True
except Exception:
    HAVE_GP = False

# Rotate every frame this many degrees CLOCKWISE before serving/saving. Sony
# liveview + capture come out in the sensor's orientation, so a window-mounted
# body usually needs 90/180/270. 0 = leave as-is.
CAMERA_ROTATE = int(os.environ.get("CAMERA_ROTATE", "0"))

# Max width/height (px) for liveview frames served to phones. Downscaling keeps
# the preview light so once-a-second polling doesn't choke a mobile browser.
PREVIEW_MAX = int(os.environ.get("PREVIEW_MAX", "800"))

# HDMI capture card (e.g. an Elgato Cam Link) for smooth, video-rate preview.
# Set CAM_DEVICE to its V4L2 node (e.g. /dev/video0) to read the live preview
# from the card via ffmpeg at 30 fps instead of slow gphoto2 liveview. Captures
# still come from the camera over USB at full resolution. Empty = gphoto2/mock.
CAM_DEVICE = os.environ.get("CAM_DEVICE", "")
CAM_FORMAT = os.environ.get("CAM_FORMAT", "mjpeg")
CAM_SIZE = os.environ.get("CAM_SIZE", "1280x720")
CAM_FPS = os.environ.get("CAM_FPS", "30")
CAM_QUALITY = os.environ.get("CAM_QUALITY", "7")  # ffmpeg mjpeg -q:v (2=best)

CAPTURE_DIR = os.path.join("data", "captures")

# One body, one shutter: every real camera command is serialized.
_camera_lock = threading.Lock()

# Persistent libgphoto2 handle (library mode only).
_gp_cam = None


def _gp_handle():
    """Open (once) and return the persistent camera handle."""
    global _gp_cam
    if _gp_cam is None:
        cam = gp.Camera()
        cam.init()
        _gp_cam = cam
    return _gp_cam


def _gp_reset() -> None:
    """Drop the handle so it's re-opened next time (after any error)."""
    global _gp_cam
    if _gp_cam is not None:
        try:
            _gp_cam.exit()
        except Exception:
            pass
    _gp_cam = None

# Preview cache, guarded by its own lock so polling clients share one frame.
_preview_lock = threading.Lock()
_preview_ts = 0.0
_preview_data = None  # type: Optional[bytes]


def _ensure_dirs() -> None:
    os.makedirs(CAPTURE_DIR, exist_ok=True)


def _rotate_bytes(data: bytes) -> bytes:
    """Rotate a JPEG by CAMERA_ROTATE degrees clockwise. No-op when 0."""
    if CAMERA_ROTATE % 360 == 0 or not data:
        return data
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img = img.rotate(-CAMERA_ROTATE, expand=True)  # PIL rotates CCW; negate
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=88)
    return out.getvalue()


def _process_preview(data: bytes) -> bytes:
    """Rotate + downscale a liveview frame so it's light for phones to poll."""
    if not data:
        return data
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if CAMERA_ROTATE % 360 != 0:
            img = img.rotate(-CAMERA_ROTATE, expand=True)
        img.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=70)
        return out.getvalue()
    except Exception:
        return _rotate_bytes(data)


def _rotate_file(path: str) -> None:
    if CAMERA_ROTATE % 360 == 0:
        return
    with open(path, "rb") as f:
        data = f.read()
    rotated = _rotate_bytes(data)
    if rotated is not data:
        with open(path, "wb") as f:
            f.write(rotated)


def _placeholder(label: str, tone: str = "capture") -> bytes:
    """Generate a placeholder JPEG that looks vaguely like a lit sidewalk."""
    from PIL import Image, ImageDraw

    w, h = 1024, 683
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    # A pool of streetlight light on the ground.
    cx, cy = w // 2, int(h * 0.62)
    for i, r in enumerate(range(220, 0, -28)):
        shade = max(0, 70 - i * 9)
        d.ellipse((cx - r, cy - r // 2, cx + r, cy + r // 2),
                  fill=(shade, shade, max(shade - 10, 0)))
    # The streetlight itself, top center, with an orange glow.
    d.line((cx, 0, cx, 150), fill=(40, 40, 40), width=6)
    d.ellipse((cx - 26, 130, cx + 26, 182), fill=(255, 106, 0))
    d.text((24, 24), "NIGHTSPOT // {}".format(tone.upper()), fill=(255, 255, 255))
    d.text((24, h - 36), label, fill=(200, 200, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def capture(session_id: str, take: int) -> str:
    """Fire the shutter and download the frame.

    Saved to data/captures/{session}-{take}.jpg and kept forever. Idempotent
    per (session, take): if the file already exists it is returned untouched,
    so a duplicate shutter request is a no-op rather than a second exposure.
    Returns the path to the JPEG.
    """
    _ensure_dirs()
    path = os.path.join(CAPTURE_DIR, "{}-{}.jpg".format(session_id, take))
    if os.path.exists(path):
        return path
    with _camera_lock:
        # Re-check inside the lock: another thread may have just shot this take.
        if os.path.exists(path):
            return path
        if MOCK:
            data = _placeholder("take {} - {}".format(take, session_id), "capture")
            with open(path, "wb") as f:
                f.write(data)
            _rotate_file(path)
            return path
        if HAVE_GP:
            # Shoot via the held handle. The image stays on the card (the
            # camera's PC+Camera save dest) — same effect as gphoto2 --keep.
            try:
                cam = _gp_handle()
                fp = cam.capture(gp.GP_CAPTURE_IMAGE)
                cf = cam.file_get(fp.folder, fp.name, gp.GP_FILE_TYPE_NORMAL)
                cf.save(path)
            except Exception:
                _gp_reset()
                raise
            if not os.path.exists(path):
                raise RuntimeError("capture produced no file")
            _rotate_file(path)
            return path
        # Subprocess fallback. gphoto2 downloads directly to our target
        # filename. --keep leaves an archive copy on the card; --force-overwrite
        # avoids prompts.
        cmd = [
            "gphoto2",
            "--capture-image-and-download",
            "--keep",
            "--force-overwrite",
            "--filename", path,
        ]
        subprocess.run(cmd, timeout=CAPTURE_TIMEOUT, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not os.path.exists(path):
            raise RuntimeError("gphoto2 reported success but no file was written")
        _rotate_file(path)
        return path


def _pull_preview_frame() -> Optional[bytes]:
    """Pull a single fresh liveview frame from the camera (or mock)."""
    if MOCK:
        return _placeholder("live - {}".format(int(time.time())), "preview")
    if HAVE_GP:
        # Held-open camera: this is a fast in-process call (~tens of ms), which
        # is what makes the preview smooth.
        cam = _gp_handle()
        cf = cam.capture_preview()
        return bytes(memoryview(cf.get_data_and_size()))
    proc = subprocess.run(
        ["gphoto2", "--capture-preview", "--stdout"],
        timeout=CAPTURE_TIMEOUT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


_worker_lock = threading.Lock()
_worker_started = False


def _refresh_preview_once() -> None:
    """Pull one liveview frame into the cache, yielding to any capture so the
    shutter is never delayed."""
    global _preview_ts, _preview_data
    if not _camera_lock.acquire(blocking=False):
        return  # a capture is in progress; skip this round
    try:
        data = _pull_preview_frame()
    except Exception:
        _gp_reset()  # drop a bad handle so it's reopened next round
        data = None
    finally:
        _camera_lock.release()
    if data is not None:
        data = _process_preview(data)
        with _preview_lock:
            _preview_ts = time.monotonic()
            _preview_data = data


def _preview_worker() -> None:
    # Library mode holds the camera open, so loop nearly continuously for a
    # smooth feed. Subprocess/mock mode pays a per-frame open cost, so pace it
    # to PREVIEW_TTL to avoid hammering the camera/CPU.
    fast = HAVE_GP and not MOCK
    while True:
        _refresh_preview_once()
        time.sleep(0.03 if fast else PREVIEW_TTL)


def _ffmpeg_vf() -> str:
    """Build the ffmpeg video filter: rotate (CAMERA_ROTATE) then downscale."""
    parts = []
    r = CAMERA_ROTATE % 360
    if r == 90:
        parts.append("transpose=1")          # 90 clockwise
    elif r == 180:
        parts.append("transpose=2,transpose=2")
    elif r == 270:
        parts.append("transpose=2")          # 90 counter-clockwise
    parts.append("scale={m}:{m}:force_original_aspect_ratio=decrease".format(
        m=PREVIEW_MAX))
    return ",".join(parts)


def _v4l2_worker() -> None:
    """Read the HDMI capture card (CAM_DEVICE) via ffmpeg at video rate, caching
    each rotated+downscaled JPEG frame. Smooth preview without touching the USB
    camera (which stays free for full-res captures)."""
    global _preview_data, _preview_ts
    while True:
        proc = None
        try:
            proc = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-nostdin",
                 "-f", "v4l2", "-input_format", CAM_FORMAT,
                 "-video_size", CAM_SIZE, "-framerate", CAM_FPS,
                 "-i", CAM_DEVICE,
                 "-vf", _ffmpeg_vf(), "-f", "mjpeg", "-q:v", CAM_QUALITY, "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            buf = b""
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi == -1:
                        break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi == -1:
                        if soi > 0:
                            buf = buf[soi:]  # drop junk before a frame start
                        break
                    with _preview_lock:
                        _preview_data = buf[soi:eoi + 2]
                        _preview_ts = time.monotonic()
                    buf = buf[eoi + 2:]
        except Exception:
            pass
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
        time.sleep(1.0)  # ffmpeg exited (e.g. no signal) — retry


def start_preview() -> None:
    """Start refreshing the preview cache in the background, so /preview.jpg is
    served instantly and a slow camera pull never blocks a web request."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    # Prefer the HDMI capture card (smooth) when CAM_DEVICE is set; otherwise
    # fall back to gphoto2/mock liveview.
    target = _v4l2_worker if (CAM_DEVICE and not MOCK) else _preview_worker
    threading.Thread(target=target, daemon=True).start()


def preview() -> Optional[bytes]:
    """Return the most recent cached liveview frame instantly (or None)."""
    with _preview_lock:
        return _preview_data
