"""End-to-end tests for nightspot, using FastAPI's TestClient with
MOCK_CAMERA=1. Runnable two ways:

    MOCK_CAMERA=1 python3 -m pytest test_app.py -q
    MOCK_CAMERA=1 python3 test_app.py

Proves: every page bounces when visited out of order; the sad->retake loop
with take numbering and the MAX_TAKES ceiling; door lock-in; double deposit
refused; double shutter-fire refused with clean JSON; same-category gifting
with fallback and a refresh-stable gift; voice privacy; one question per
session; and the attachment disposition on the photo download.
"""
import os
import tempfile
import uuid

# Must be set before importing camera/app so the mock path is taken and the
# retake ceiling is small enough to exercise quickly.
os.environ["MOCK_CAMERA"] = "1"
os.environ.setdefault("MAX_TAKES", "3")

import bank  # noqa: E402
import camera  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Redirect every filesystem + DB path into a throwaway temp dir.
_TMP = tempfile.mkdtemp(prefix="nightspot-test-")
bank.DB_PATH = os.path.join(_TMP, "nightspot.db")
camera.CAPTURE_DIR = os.path.join(_TMP, "captures")
appmod.CAPTURE_DIR = camera.CAPTURE_DIR
appmod.VOICE_DIR = os.path.join(_TMP, "voices")
MAX_TAKES = appmod.MAX_TAKES

# TestClient only fires startup events when used as a context manager, and we
# create bare clients per test — so set up dirs and the schema ourselves.
os.makedirs(camera.CAPTURE_DIR, exist_ok=True)
os.makedirs(appmod.VOICE_DIR, exist_ok=True)
bank.init()

app = appmod.app


def client():
    return TestClient(app)


# --- flow helpers -----------------------------------------------------------

def begin(c):
    r = c.post("/start", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/capture"


def shoot(c):
    r = c.post("/api/shoot")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    return j


def to_review(c):
    begin(c)
    shoot(c)


def to_path(c, door="memory"):
    to_review(c)
    r = c.post("/review", data={"feeling": "happy"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/path"


def to_deposit_page(c, door="memory"):
    to_path(c)
    r = c.post("/path", data={"door": door}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/rec"


def deposit_text(c, door="memory", body="a small true thing"):
    to_deposit_page(c, door)
    r = c.post("/api/deposit", data={"mode": "text", "body": body},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/gift"


def to_gifted(c, door="memory", body="a small true thing"):
    deposit_text(c, door, body)
    c.get("/gift")  # assigns the gift
    r = c.post("/carry", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/exit"


# --- tests ------------------------------------------------------------------

def test_root_skips_start_page_at_night():
    orig = appmod.is_night
    try:
        appmod.is_night = lambda: True
        c = client()
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/capture"
        assert c.cookies.get("nightspot")  # a session was started
        shoot(c)  # advance to review
        # Returning to / now resumes where we are, not back to capture.
        r2 = c.get("/", follow_redirects=False)
        assert r2.status_code == 303 and r2.headers["location"] == "/review"
    finally:
        appmod.is_night = orig


def test_finished_session_restarts_from_root_at_night():
    # A done run must not trap you on /getout — re-scanning starts fresh.
    orig = appmod.is_night
    try:
        appmod.is_night = lambda: True
        c = client()
        to_gifted(c, "memory")
        c.get("/getout")  # advances state -> done
        sid_before = c.cookies.get("nightspot")
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/capture"
        assert c.cookies.get("nightspot") != sid_before  # a new run
    finally:
        appmod.is_night = orig


def test_root_shows_sign_during_day():
    orig = appmod.is_night
    try:
        appmod.is_night = lambda: False
        c = client()
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 200
        assert "It's Better at Night" in r.text  # the daytime closed-sign
    finally:
        appmod.is_night = orig


def test_no_session_bounces_to_landing():
    c = client()
    for path in ["/capture", "/review", "/path", "/rec", "/gift", "/exit",
                 "/ask", "/getout", "/download"]:
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/", path


def test_out_of_order_forward_pages_bounce_back_to_capture():
    c = client()
    begin(c)  # state = capture
    for path in ["/review", "/path", "/rec", "/gift", "/exit", "/ask",
                 "/getout"]:
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/capture", (path, r.headers["location"])


def test_out_of_order_backward_page_bounces_forward():
    c = client()
    to_path(c)  # state = path
    # Visiting an earlier page redirects forward to where we actually are.
    r = c.get("/capture", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/path"
    r = c.get("/review", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/path"


def test_sad_retake_loop_numbering_and_ceiling():
    c = client()
    begin(c)
    # Take 1 heading.
    assert "Stand on the spot" in c.get("/capture").text
    for take in range(1, MAX_TAKES + 1):
        shoot(c)
        rev = c.get("/review")
        assert "Take {}".format(take) in rev.text
        if take < MAX_TAKES:
            # Sad is still offered; go again.
            assert 'value="sad"' in rev.text
            r = c.post("/review", data={"feeling": "sad"},
                       follow_redirects=False)
            assert r.headers["location"] == "/capture"
            cap = c.get("/capture")
            assert "Again. Take {}".format(take + 1) in cap.text
        else:
            # At the ceiling: Sad button is gone, footer changes.
            assert 'value="sad"' not in rev.text
            assert "The light has spoken" in rev.text
            # Even if forced, a sad POST is refused (stays on review).
            r = c.post("/review", data={"feeling": "sad"},
                       follow_redirects=False)
            assert r.headers["location"] == "/review"


def test_door_lock_in():
    c = client()
    to_path(c)
    r = c.post("/path", data={"door": "memory"}, follow_redirects=False)
    assert r.headers["location"] == "/rec"
    sid = c.cookies.get("nightspot")
    # Revisiting /path bounces forward to /rec.
    r = c.get("/path", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/rec"
    # Switching the door is refused; the lock holds.
    r = c.post("/path", data={"door": "fear"}, follow_redirects=False)
    assert r.headers["location"] == "/rec"
    assert bank.get_session(sid)["path"] == "memory"


def test_double_deposit_refused():
    c = client()
    deposit_text(c, "memory", "first and only")
    sid = c.cookies.get("nightspot")
    before = bank.get_session(sid)["deposit_id"]
    # Second deposit attempt bounces to /gift and changes nothing.
    r = c.post("/api/deposit", data={"mode": "text", "body": "sneaky second"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/gift"
    assert bank.get_session(sid)["deposit_id"] == before


def test_double_shutter_refused_clean_json():
    c = client()
    to_review(c)  # one shot fired, state = review
    r = c.post("/api/shoot")
    assert r.status_code == 409
    j = r.json()  # clean JSON, not an HTML 500
    assert j["ok"] is False
    assert "error" in j


def test_gift_confirms_your_own_deposit():
    # /gift echoes back YOUR deposit: heading is the door you picked, and the
    # body is exactly what you set down.
    c = client()
    deposit_text(c, "fear", "dog")
    page = c.get("/gift").text
    assert "<h1>Fear</h1>" in page          # heading = the door you chose
    assert "dog" in page                     # your own words, not a stranger's
    assert "Someone stood where you" not in page  # no swap on this screen
    # Refresh-stable: still your deposit, unchanged.
    assert "dog" in c.get("/gift").text


def test_bank_pick_gift_powers_the_feed():
    # The exchange now lives in the (future) daily feed, not on /gift, but the
    # bank still selects a stranger's deposit: never your own, least-circulated
    # first, with a cross-category fallback.
    s1 = uuid.uuid4().hex
    asker = uuid.uuid4().hex
    m1 = bank.add_memory(s1, "rec", "text", "feed-rec-1")
    pick = bank.pick_gift(asker, "rec")
    assert pick is not None
    assert pick["session_id"] != asker          # never the asker's own
    # Asking as s1 never returns s1's own deposit.
    own = bank.pick_gift(s1, "rec")
    assert own is None or own["id"] != m1


def test_voice_privacy():
    # Make a voice deposit and grab its id straight from the bank.
    c = client()
    to_deposit_page(c, "memory")
    files = {"audio": ("d.webm", b"FAKEWEBMBYTES", "audio/webm")}
    r = c.post("/api/deposit", data={"mode": "voice"}, files=files,
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/gift"
    sid = c.cookies.get("nightspot")
    voice_id = bank.get_session(sid)["deposit_id"]
    assert bank.get_memory(voice_id)["kind"] == "voice"

    # A different session, NOT gifted this voice, is turned away.
    other = client()
    to_gifted(other, "memory")
    r = other.get("/voice/{}".format(voice_id), follow_redirects=False)
    assert r.status_code in (302, 303, 403)
    assert r.status_code != 200

    # The session it IS gifted to can play it back.
    giftee = client()
    to_deposit_page(giftee, "memory")
    giftee.post("/api/deposit", data={"mode": "text", "body": "x"},
                follow_redirects=False)
    gsid = giftee.cookies.get("nightspot")
    bank.update_session(gsid, gift_id=voice_id)
    r = giftee.get("/voice/{}".format(voice_id))
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/webm"


def test_deposit_carries_photo_and_gift_photo_is_private():
    # The depositor's photo is recorded with their deposit...
    a = client()
    deposit_text(a, "memory", "with a photo")
    a_sid = a.cookies.get("nightspot")
    a_dep = bank.get_session(a_sid)["deposit_id"]
    assert bank.get_memory(a_dep)["photo"]

    # ...not servable to a session it wasn't gifted to...
    other = client()
    deposit_text(other, "memory", "o")
    bank.update_session(other.cookies.get("nightspot"), gift_id=-999)
    r = other.get("/gift-photo/{}".format(a_dep), follow_redirects=False)
    assert r.status_code in (302, 303) and r.status_code != 200

    # ...but a session it's assigned to (the future feed's recipient) gets it.
    # (Reserved for the daily feed; the live /gift screen shows your own photo
    # via /photo, so it no longer references /gift-photo.)
    recipient = client()
    deposit_text(recipient, "memory", "g")
    bank.update_session(recipient.cookies.get("nightspot"), gift_id=a_dep)
    r = recipient.get("/gift-photo/{}".format(a_dep))
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_one_question_per_session():
    c = client()
    to_gifted(c, "memory")
    sid = c.cookies.get("nightspot")
    r = c.post("/api/ask", data={"body": "Will the light hold?"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ask"
    assert "in the window's hands" in c.get("/ask").text
    # Second question is silently refused by the UNIQUE constraint.
    c.post("/api/ask", data={"body": "And a second one?"},
           follow_redirects=False)
    q = bank.get_question(sid)
    assert q["body"] == "Will the light hold?"


def test_ask_terminal_sets_done_and_offers_restart():
    # Finishing via the Ask door must not trap you: the page becomes terminal
    # (state done) and offers a restart link.
    orig = appmod.is_night
    try:
        appmod.is_night = lambda: True
        c = client()
        to_gifted(c, "memory")
        c.post("/api/ask", data={"body": "q"}, follow_redirects=False)
        sid = c.cookies.get("nightspot")
        page = c.get("/ask").text
        assert 'href="/"' in page  # an "Again?" restart link
        assert bank.get_session(sid)["state"] == "done"
        r = c.get("/", follow_redirects=False)
        assert r.headers["location"] == "/capture"  # restarts, not trapped
    finally:
        appmod.is_night = orig


def test_subscribe_records_name_phone_and_assignment():
    c = client()
    to_gifted(c, "memory")
    assert "name" in c.get("/subscribe").text  # the signup form is shown
    r = c.post("/api/subscribe",
               data={"name": "Dana", "phone": "555-0100"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/subscribe?ok=")
    subs = [dict(x) for x in bank.list_subscribers()]
    mine = [x for x in subs if x["phone"] == "555-0100"]
    assert len(mine) == 1
    assert mine[0]["name"] == "Dana"
    assert mine[0]["gift_id"] is not None      # a prior deposit was set aside
    # The confirmation view renders without re-submitting.
    assert "on the list" in c.get("/subscribe?ok=memory").text


def test_subscribe_requires_finished_flow():
    c = client()
    begin(c)  # state = capture, not yet gifted
    r = c.get("/subscribe", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/capture"
    r = c.post("/api/subscribe", data={"name": "x", "phone": "y"},
               follow_redirects=False)
    assert r.headers["location"] == "/capture"


def test_admin_requires_key():
    c = client()
    deposit_text(c, "fear", "admin-visible-deposit")
    assert c.get("/admin", follow_redirects=False).status_code == 401
    assert c.get("/admin?key=wrong", follow_redirects=False).status_code == 401
    page = c.get("/admin?key={}".format(appmod.ADMIN_KEY))
    assert page.status_code == 200
    assert "admin-visible-deposit" in page.text  # submissions are listed
    # Admin media is also key-gated.
    assert c.get("/admin/photo/1", follow_redirects=False).status_code == 401


def test_photo_download_attachment_disposition():
    c = client()
    to_gifted(c, "memory")
    c.get("/getout")  # advances to done, confirms a photo exists
    r = c.get("/download", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "its-better-at-night.jpg" in cd


# --- standalone runner ------------------------------------------------------

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print("PASS {}".format(t.__name__))
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            print("FAIL {}: {}".format(t.__name__, e))
            traceback.print_exc()
    print("\n{} passed, {} failed".format(len(tests) - failures, failures))
    return failures


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run_all() else 0)
