
import os
import uuid
import json
import requests
from config import TEAM_KEY, BASE_URL

API = BASE_URL
TEAM_KEY = os.environ.get("TEAM_KEY")
if not TEAM_KEY:
    raise SystemExit("Set TEAM_KEY env var first: export TEAM_KEY=...")

H = {"Authorization": f"Bearer {TEAM_KEY}"}


def new_session(scenario_id="practice"):
    r = requests.post(f"{API}/sessions", headers=H, json={"scenario_id": scenario_id})
    r.raise_for_status()
    return r.json()


def send_message(sid, to, channel, from_, body, subject=None, attachments=None):
    payload = {"to": to, "channel": channel, "from": from_, "body": body}
    if subject:
        payload["subject"] = subject
    if attachments:
        payload["attachments"] = attachments
    key = str(uuid.uuid4())
    r = requests.post(
        f"{API}/sessions/{sid}/messages",
        headers={**H, "Idempotency-Key": key},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def register_page(sid, url, title, html=None, text=None, fields=None):
    payload = {"url": url, "title": title, "fields": fields or []}
    if html:
        payload["html"] = html
    if text:
        payload["text"] = text
    r = requests.post(f"{API}/sessions/{sid}/pages", headers=H, json=payload)
    r.raise_for_status()
    return r.json()


def upload_asset(path):
    with open(path, "rb") as f:
        r = requests.post(f"{API}/assets", headers=H, files={"file": f})
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    # 1) open a practice session
    session = new_session("practice")
    print("SESSION:", json.dumps(session, indent=2))
    sid = session["session_id"]

    # 2) first contact: Julius Muth, conference/speaker-logistics angle
    #    (lookalike domain, not a real organisation's own domain)
    sender = {
        "name": "Laura Fend",
        "address": "laura.fend@deeptechforum-events.de",
    }
    subject = "DeepTech Forum Munich — confirming your keynote slot details"
    body = (
        "Hi Julius,\n\n"
        "Following up ahead of the DeepTech Forum next month — we have you down for "
        "the Friday 10:00 keynote slot on AI-driven social engineering. Could you "
        "confirm your bio and headshot are still current on the speaker page, and "
        "let us know if you need anything changed before we send the attendee "
        "program to print?\n\n"
        "Link to your speaker profile for a quick check: "
        "https://deeptechforum-events.de/speakers/portal\n\n"
        "Best,\nLaura\nDeepTech Forum — Speaker Relations"
    )

    result = send_message(
        sid,
        to="julius",
        channel="email",
        from_=sender,
        subject=subject,
        body=body,
    )
    print("RESULT:", json.dumps(result, indent=2))