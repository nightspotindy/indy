"""nightspot — an interactive photo-spot installation.

A Sony a7 III in a second-story window points at a sidewalk spot under a
streetlight. Visitors scan a QR code and move through a server-enforced,
locked flow on their phone:

    start -> capture -> review -> path -> rec -> deposited -> gifted -> done

The flow is a state machine stored in SQLite and keyed to a session cookie
(1 hour TTL). No skipping ahead, no going back, no replays: any out-of-order
request is redirected to wherever the session actually is.
"""
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

def _load_env_file(path: str) -> None:
    """Load KEY=VALUE lines from a local env file (gitignored secrets like
    SMTP_PASS) into the environment if it exists. Real env vars already set
    (e.g. systemd Environment=) take precedence. Runs before importing modules
    that read their config at import time."""
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(),
                                      v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "nightspot.env"))

import bank  # noqa: E402  (after the env file is loaded)
import camera  # noqa: E402
import notify  # noqa: E402

VERSION = "v17"

# Night window (local time). Night spans NIGHT_START..midnight..NIGHT_END.
NIGHT_START = int(os.environ.get("NIGHT_START", "20"))
NIGHT_END = int(os.environ.get("NIGHT_END", "6"))

MAX_TAKES = int(os.environ.get("MAX_TAKES", "5"))
SESSION_TTL = 3600  # one hour
COOKIE = "nightspot"

# A Give/tip door is intentionally shelved for now. Leave the URL here and a
# commented path in /exit so it can be restored later.
TIP_URL = os.environ.get("TIP_URL", "")

# Admin dashboard key. Override in production: ADMIN_KEY=... Access the
# dashboard at /admin?key=<ADMIN_KEY>.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "nightspot")

CAPTURE_DIR = os.path.join("data", "captures")
VOICE_DIR = os.path.join("data", "voices")

# The three doors. Key is the locked path value AND the bank category.
DOORS = {
    "rec": {
        "button": "Recommendation",
        "heading": "Recommendation",
        "tagline": "Who's got it better than us?",
        "kept": "You recommended this.",
        "gift_lede": "Someone stood where you're standing and recommended this.",
    },
    "memory": {
        "button": "Memory",
        "heading": "Memory",
        "tagline": "I offer you the memory of a yellow rose seen at sunset "
                   "long before you were born",
        "kept": "You left this behind.",
        "gift_lede": "Someone stood where you're standing and left this behind.",
    },
    "fear": {
        "button": "Fear",
        "heading": "Fear",
        "tagline": "It's okay to be existential sometimes",
        "kept": "You set this down.",
        "gift_lede": "Someone stood where you're standing and set this down.",
    },
}

# Canonical URL for each state — where a session "actually is". Out-of-order
# requests redirect here.
STATE_URL = {
    "start": "/",
    "capture": "/capture",
    "review": "/review",
    "path": "/path",
    "rec": "/rec",
    "deposited": "/gift",
    "gifted": "/exit",
    "done": "/getout",
}

class _Templates(Jinja2Templates):
    """Render via the modern (request, name, context) argument order so the
    app works on both older Starlette (dev Mac, which accepts either order)
    and newer Starlette (the Pi runs 1.3.x, which removed the legacy
    (name, context) form). All call sites still pass (name, context)."""

    def TemplateResponse(self, name, context=None, *args, **kwargs):
        context = context or {}
        request = context.get("request")
        return super().TemplateResponse(request, name, context, *args, **kwargs)


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = _Templates(directory="templates")


@app.on_event("startup")
def _startup() -> None:
    # Surface nightspot's own logs (notably email-alert success/failure) on
    # stderr so they show up in uvicorn output / journalctl on the Pi.
    nlog = logging.getLogger("nightspot")
    if not nlog.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        nlog.addHandler(h)
    nlog.setLevel(logging.INFO)
    nlog.propagate = False
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    os.makedirs(VOICE_DIR, exist_ok=True)
    bank.init()


# --- helpers ----------------------------------------------------------------

def is_night() -> bool:
    hour = time.localtime().tm_hour
    if NIGHT_START <= NIGHT_END:
        return NIGHT_START <= hour < NIGHT_END
    # Window wraps past midnight (the normal case: 20..6).
    return hour >= NIGHT_START or hour < NIGHT_END


def current_session(request: Request) -> Optional[dict]:
    """Return the live session as a dict, or None if absent/expired."""
    sid = request.cookies.get(COOKIE)
    if not sid:
        return None
    row = bank.get_session(sid)
    if row is None:
        return None
    if time.time() - row["created_at"] > SESSION_TTL:
        return None
    return dict(row)


def redirect_to_state(state: str) -> RedirectResponse:
    return RedirectResponse(STATE_URL.get(state, "/"), status_code=303)


def ctx(request: Request, **kw) -> dict:
    base = {"request": request, "version": VERSION, "is_night": is_night()}
    base.update(kw)
    return base


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


def latest_take_path(session_id: str, takes: int) -> Optional[str]:
    if takes <= 0:
        return None
    path = os.path.join(CAPTURE_DIR, "{}-{}.jpg".format(session_id, takes))
    return path if os.path.exists(path) else None


# --- 1. landing -------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    s = current_session(request)
    if s is not None and s["state"] != "done":
        # Returning mid-flow (or after a refresh): resume where they actually
        # are. Never silently restart someone's run...
        return redirect_to_state(s["state"])
    # ...but a FINISHED run is over — the next scan of the QR begins fresh
    # instead of trapping you on the get-out page.
    if is_night():
        # Night: skip the start page entirely — scanning the QR drops you
        # straight onto the spot.
        sid = uuid.uuid4().hex
        bank.create_session(sid, state="capture")
        resp = RedirectResponse("/capture", status_code=303)
        resp.set_cookie(COOKIE, sid, max_age=SESSION_TTL, httponly=True,
                        samesite="lax")
        return resp
    # Daytime: the "it's better at night" sign stays up.
    return templates.TemplateResponse("landing.html", ctx(request))


@app.post("/start")
def start(request: Request):
    """Begin a fresh run: new session, state 'capture'."""
    sid = uuid.uuid4().hex
    bank.create_session(sid, state="capture")
    resp = RedirectResponse("/capture", status_code=303)
    resp.set_cookie(COOKIE, sid, max_age=SESSION_TTL, httponly=True,
                    samesite="lax")
    return resp


# --- 2. capture -------------------------------------------------------------

@app.get("/capture", response_class=HTMLResponse)
def capture_page(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] != "capture":
        return redirect_to_state(s["state"])
    take_n = s["takes"] + 1
    heading = "Stand on the spot" if take_n == 1 else "Again. Take {}".format(take_n)
    return templates.TemplateResponse(
        "capture.html", ctx(request, heading=heading, take_n=take_n))


@app.post("/api/shoot")
def api_shoot(request: Request):
    """Fire the shutter. Returns clean JSON; refuses a double fire."""
    s = current_session(request)
    if s is None:
        return JSONResponse({"ok": False, "error": "no session"}, status_code=409)
    if s["state"] != "capture":
        # Already shot (or not at capture): refuse, don't expose a second frame.
        return JSONResponse({"ok": False, "error": "out of order"}, status_code=409)
    take_n = s["takes"] + 1
    try:
        camera.capture(s["id"], take_n)
    except Exception as e:  # camera blinked
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    bank.update_session(s["id"], takes=take_n, state="review")
    return JSONResponse({"ok": True, "take": take_n})


# --- 3. review --------------------------------------------------------------

@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] != "review":
        return redirect_to_state(s["state"])
    at_cap = s["takes"] >= MAX_TAKES
    return templates.TemplateResponse(
        "review.html", ctx(request, take_n=s["takes"], at_cap=at_cap))


@app.post("/review")
def review_decide(request: Request, feeling: str = Form(...)):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] != "review":
        return redirect_to_state(s["state"])
    if feeling == "happy":
        bank.update_session(s["id"], state="path")
        return RedirectResponse("/path", status_code=303)
    # Sad -> another take, unless we've hit the ceiling.
    if s["takes"] >= MAX_TAKES:
        return redirect_to_state(s["state"])  # Sad button is gone; ignore.
    bank.update_session(s["id"], state="capture")
    return RedirectResponse("/capture", status_code=303)


# --- 4. three doors ---------------------------------------------------------

@app.get("/path", response_class=HTMLResponse)
def path_page(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] != "path":
        return redirect_to_state(s["state"])
    return templates.TemplateResponse(
        "path.html", ctx(request, doors=DOORS))


@app.post("/path")
def path_choose(request: Request, door: str = Form(...)):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    # The choice locks behind you: only choosable while at 'path' state.
    if s["state"] != "path":
        return redirect_to_state(s["state"])
    if door not in DOORS:
        return redirect_to_state(s["state"])
    bank.update_session(s["id"], path=door, state="rec")
    return RedirectResponse("/rec", status_code=303)


# --- 5. deposit -------------------------------------------------------------

@app.get("/rec", response_class=HTMLResponse)
def rec_page(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] != "rec":
        return redirect_to_state(s["state"])
    door = DOORS[s["path"]]
    return templates.TemplateResponse(
        "rec.html", ctx(request, door=door, category=s["path"]))


@app.post("/api/deposit")
async def api_deposit(
    request: Request,
    mode: str = Form(...),
    body: str = Form(""),
    audio: Optional[UploadFile] = File(None),
):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    # Double deposit refused: only depositable while at 'rec'.
    if s["state"] != "rec":
        return redirect_to_state(s["state"])
    category = s["path"]
    # The depositor's chosen photo travels with their deposit, so whoever is
    # gifted it sees where the stranger stood — not just their words. This
    # makes each deposit a self-contained packet (photo + message) a future
    # "a text a day from a stranger" feed could deliver whole.
    photo = "{}-{}.jpg".format(s["id"], s["takes"])
    if not os.path.exists(os.path.join(CAPTURE_DIR, photo)):
        photo = None
    if mode == "voice" and audio is not None:
        data = await audio.read()
        mem_id = bank.add_memory(s["id"], category, "voice", "", photo=photo)
        fname = "{}.webm".format(mem_id)
        with open(os.path.join(VOICE_DIR, fname), "wb") as f:
            f.write(data)
        # Store the filename as the body so it can be served back later.
        _set_voice_body(mem_id, fname)
        bank.update_session(s["id"], deposit_id=mem_id, state="deposited")
        kind, summary = "voice", "(voice recording)"
    else:
        text = (body or "").strip()[:2000]
        mem_id = bank.add_memory(s["id"], category, "text", text, photo=photo)
        bank.update_session(s["id"], deposit_id=mem_id, state="deposited")
        kind, summary = "text", (text or "(empty)")
    notify.send(
        "nightspot — a {} was left".format(category),
        "Someone just left a {} ({}){}.\n\n{}\n\n{}\nSee everything in the "
        "admin dashboard.".format(
            category, kind, ", with a photo" if photo else "", summary,
            _now_str()),
    )
    return RedirectResponse("/gift", status_code=303)


def _set_voice_body(mem_id: int, fname: str) -> None:
    c = bank._conn()
    c.execute("UPDATE memories SET body=? WHERE id=?", (fname, mem_id))
    c.commit()


# --- 6. gift ----------------------------------------------------------------

@app.get("/gift", response_class=HTMLResponse)
def gift_page(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] != "deposited":
        return redirect_to_state(s["state"])
    # Confirmation: show the visitor their OWN deposit back — the door they
    # chose as the heading, and what they set down. (Every deposit still lands
    # in the bank for the future "a text a day from a stranger" feed; the
    # stranger exchange happens there, not on this screen.)
    gift = None
    if s["deposit_id"] is not None:
        gift = bank.get_memory(s["deposit_id"])
    heading = "Yours now."
    kept = ""
    if gift is not None:
        door = DOORS.get(gift["category"], DOORS["memory"])
        heading = door["heading"]
        kept = door["kept"]
    return templates.TemplateResponse(
        "gift.html", ctx(request, gift=gift, heading=heading, kept=kept))


@app.post("/carry")
def carry(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] != "deposited":
        return redirect_to_state(s["state"])
    bank.update_session(s["id"], state="gifted")
    return RedirectResponse("/exit", status_code=303)


# --- 7. exit menu -----------------------------------------------------------

@app.get("/exit", response_class=HTMLResponse)
def exit_page(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] not in ("gifted", "done"):
        return redirect_to_state(s["state"])
    # NOTE: a "Give / tip" door is still shelved. To restore it, surface
    # TIP_URL here as a button -> external tip page.
    return templates.TemplateResponse("exit.html", ctx(request))


# --- subscribe: "a text a day from a stranger" ------------------------------

@app.get("/subscribe", response_class=HTMLResponse)
def subscribe_page(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] not in ("gifted", "done"):
        return redirect_to_state(s["state"])
    # After a successful signup we land back here with ?ok=<category> so a
    # refresh can't re-submit.
    ok = request.query_params.get("ok")
    return templates.TemplateResponse(
        "subscribe.html", ctx(request, ok=ok))


@app.post("/api/subscribe")
def api_subscribe(request: Request, name: str = Form(""),
                  phone: str = Form("")):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] not in ("gifted", "done"):
        return redirect_to_state(s["state"])
    name = (name or "").strip()[:80]
    phone = (phone or "").strip()[:40]
    if not name or not phone:
        return RedirectResponse("/subscribe", status_code=303)
    # Set aside a prior stranger's deposit for them.
    chosen = bank.pick_for_subscriber()
    gift_id = chosen["id"] if chosen is not None else None
    category = chosen["category"] if chosen is not None else "memory"
    if gift_id is not None:
        bank.mark_given(gift_id)
    bank.add_subscriber(name, phone, gift_id, category)
    notify.send(
        "nightspot — new signup",
        "Name:  {}\nPhone: {}\nSet aside: a {} (#{})\n\n{}\nSee everything in "
        "the admin dashboard.".format(name, phone, category, gift_id,
                                      _now_str()),
    )
    # TODO(sms): this is where the daily feed would queue/send an MMS via a
    # provider (Twilio etc.) — name/phone/gift_id are all recorded for it.
    return RedirectResponse("/subscribe?ok={}".format(category),
                            status_code=303)


# --- admin dashboard --------------------------------------------------------

def _admin_ok(request: Request) -> bool:
    return request.query_params.get("key") == ADMIN_KEY


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    if not _admin_ok(request):
        return Response("nope", status_code=401)

    def fmt(ts):
        if not ts:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    mems = []
    for r in bank.list_memories():
        d = dict(r)
        d["when"] = fmt(d.get("created_at"))
        mems.append(d)
    questions = []
    for r in bank.list_questions():
        d = dict(r)
        d["when"] = fmt(d.get("created_at"))
        questions.append(d)
    subs = []
    for r in bank.list_subscribers():
        d = dict(r)
        d["when"] = fmt(d.get("created_at"))
        subs.append(d)
    return templates.TemplateResponse(
        "admin.html",
        ctx(request, key=ADMIN_KEY, mems=mems, questions=questions, subs=subs),
    )


@app.get("/admin/photo/{mem_id}")
def admin_photo(request: Request, mem_id: int):
    if not _admin_ok(request):
        return Response("nope", status_code=401)
    mem = bank.get_memory(mem_id)
    if mem is None or not mem["photo"]:
        return Response(status_code=404)
    path = os.path.join(CAPTURE_DIR, mem["photo"])
    if not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/admin/voice/{mem_id}")
def admin_voice(request: Request, mem_id: int):
    if not _admin_ok(request):
        return Response("nope", status_code=401)
    mem = bank.get_memory(mem_id)
    if mem is None or mem["kind"] != "voice" or not mem["body"]:
        return Response(status_code=404)
    path = os.path.join(VOICE_DIR, mem["body"])
    if not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="audio/webm")


# --- 8. ask -----------------------------------------------------------------

@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] not in ("gifted", "done"):
        return redirect_to_state(s["state"])
    asked = bank.get_question(s["id"]) is not None
    if asked and s["state"] != "done":
        # The question was the last act: this is now a terminal page, so a
        # reload of / restarts cleanly instead of trapping here.
        bank.update_session(s["id"], state="done")
    has_photo = latest_take_path(s["id"], s["takes"]) is not None
    return templates.TemplateResponse(
        "ask.html", ctx(request, asked=asked, has_photo=has_photo))


@app.post("/api/ask")
def api_ask(request: Request, body: str = Form("")):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] not in ("gifted", "done"):
        return redirect_to_state(s["state"])
    text = (body or "").strip()[:500]
    if text and bank.add_question(s["id"], text):  # UNIQUE: one per session
        notify.send(
            "nightspot — a question for the window",
            "{}\n\n{}\nSee everything in the admin dashboard.".format(
                text, _now_str()),
        )
    return RedirectResponse("/ask", status_code=303)


# --- 9. get out -------------------------------------------------------------

@app.get("/getout", response_class=HTMLResponse)
def getout_page(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["state"] not in ("gifted", "done"):
        return redirect_to_state(s["state"])
    if s["state"] != "done":
        bank.update_session(s["id"], state="done")
    has_photo = latest_take_path(s["id"], s["takes"]) is not None
    return templates.TemplateResponse(
        "getout.html", ctx(request, has_photo=has_photo))


@app.get("/download")
def download(request: Request):
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    path = latest_take_path(s["id"], s["takes"])
    if path is None:
        return RedirectResponse("/", status_code=303)
    return FileResponse(
        path,
        media_type="image/jpeg",
        filename="its-better-at-night.jpg",
        headers={"Content-Disposition":
                 'attachment; filename="its-better-at-night.jpg"'},
    )


# --- photo of the take just shot (review page) ------------------------------

@app.get("/photo")
def photo(request: Request):
    s = current_session(request)
    if s is None:
        return Response(status_code=404)
    path = latest_take_path(s["id"], s["takes"])
    if path is None:
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


# --- live preview -----------------------------------------------------------

@app.get("/preview.jpg")
def preview_jpg():
    frame = camera.preview()
    if frame is None:
        return Response(status_code=204)
    return Response(content=frame, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# --- gifted deposit's photo (privacy-gated) ---------------------------------

@app.get("/gift-photo/{mem_id}")
def gift_photo(request: Request, mem_id: int):
    """Serve the photo a deposit carries, but ONLY to its giftee."""
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    if s["gift_id"] != mem_id:
        return RedirectResponse("/", status_code=303)
    mem = bank.get_memory(mem_id)
    if mem is None or not mem["photo"]:
        return Response(status_code=404)
    path = os.path.join(CAPTURE_DIR, mem["photo"])
    if not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


# --- voice playback (privacy-gated) -----------------------------------------

@app.get("/voice/{mem_id}")
def voice(request: Request, mem_id: int):
    """Serve a voice deposit, but ONLY to the session it was gifted to."""
    s = current_session(request)
    if s is None:
        return RedirectResponse("/", status_code=303)
    mem = bank.get_memory(mem_id)
    if mem is None or mem["kind"] != "voice":
        return Response(status_code=404)
    # Servable to the owner (their own deposit, echoed back on /gift) or to the
    # session it's been gifted to; everyone else is sent away.
    if mem["session_id"] != s["id"] and s["gift_id"] != mem_id:
        return RedirectResponse("/", status_code=303)
    path = os.path.join(VOICE_DIR, mem["body"])
    if not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="audio/webm",
                        headers={"Cache-Control": "no-store"})
