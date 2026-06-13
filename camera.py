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

# Rotate every frame this many degrees CLOCKWISE before serving/saving. Sony
# liveview + capture come out in the sensor's orientation, so a window-mounted
# body usually needs 90/180/270. 0 = leave as-is.
CAMERA_ROTATE = int(os.environ.get("CAMERA_ROTATE", "0"))

CAPTURE_DIR = os.path.join("data", "captures")

# One body, one shutter: every real camera command is serialized.
_camera_lock = threading.Lock()

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
        # gphoto2 downloads directly to our target filename. --keep leaves an
        # archive copy on the camera's card; --force-overwrite avoids prompts.
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
    proc = subprocess.run(
        ["gphoto2", "--capture-preview", "--stdout"],
        timeout=CAPTURE_TIMEOUT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def preview() -> Optional[bytes]:
    """Return a recent liveview JPEG, or None if unavailable.

    Throttled server-side to one real pull per PREVIEW_TTL seconds; within
    that window every caller gets the cached frame. The capture lock is
    acquired NON-blocking, so if a real capture is in progress the preview
    yields immediately and serves the last cached frame instead of waiting.
    """
    global _preview_ts, _preview_data
    now = time.monotonic()
    with _preview_lock:
        cached = _preview_data
        fresh = cached is not None and (now - _preview_ts) < PREVIEW_TTL
    if fresh:
        return cached

    # Time for a new pull, but never delay a capture for it.
    if not _camera_lock.acquire(blocking=False):
        return cached
    try:
        data = _pull_preview_frame()
    except Exception:
        data = None
    finally:
        _camera_lock.release()

    if data is not None:
        data = _rotate_bytes(data)
        with _preview_lock:
            _preview_ts = time.monotonic()
            _preview_data = data
        return data
    return cached
