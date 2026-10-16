"""Run the Webex bot page in a local Chrome without Docker, Django, Xvfb or GStreamer.

It plays the role of WebBotAdapter: it serves the page, runs the websocket server the page reports to, and prints
every message the page sends. Captured audio is saved as wav files.

    # Load the page and the SDK without joining. Needs no credentials.
    python bots/webex_bot_adapter/dev/standalone_harness.py

    python bots/webex_bot_adapter/dev/standalone_harness.py --meeting-url <url or meeting number> [--password <password>]

Credentials come from the environment or from a .env file at the repo root:
    WEBEX_CLIENT_ID, WEBEX_CLIENT_SECRET, plus either
    WEBEX_SERVICE_APP_REFRESH_TOKEN, or
    WEBEX_SERVICE_APP_ID, WEBEX_TARGET_ORG_ID and WEBEX_PERSONAL_ACCESS_TOKEN to issue the first tokens.
"""

import argparse
import importlib.util
import json
import os
import signal
import sys
import threading
import time
import wave
from collections import Counter, defaultdict

import numpy as np
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from websockets.sync.server import serve

ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(ADAPTER_DIR))
sys.path.insert(0, ADAPTER_DIR)

from webex_guest_token import create_guest_token, issue_service_app_tokens, refresh_service_app_tokens  # noqa: E402

PAGE_FILES = {"/webex_chromedriver_page.html", "/webex_chromedriver_page.js"}
STATIC_PAGE_SERVER_PATH = os.path.join(ADAPTER_DIR, "..", "web_bot_adapter", "static_page_server.py")
SHARED_PAYLOAD_PATH = os.path.join(ADAPTER_DIR, "..", "web_bot_adapter", "shared_chromedriver_payload.js")
PAYLOAD_PATH = os.path.join(ADAPTER_DIR, "webex_chromedriver_payload.js")

MESSAGE_TYPE_JSON = 1
MESSAGE_TYPE_MIXED_AUDIO = 3
MESSAGE_TYPE_PER_PARTICIPANT_AUDIO = 5

AUDIO_SAMPLE_RATE = 48000
MEETING_OVER_CHANGES = ("removed_from_meeting", "meeting_ended")
# The webcam is published before the tone starts, so each can be observed on its own
BOT_OUTPUT_SETTLE_SECONDS = 3
SCREENSHOT_DELAY_SECONDS = 20


class BotMessages:
    """What the page sent over the websocket, as WebBotAdapter.handle_websocket receives it."""

    def __init__(self):
        self.json_messages = []
        self.binary_counts = Counter()
        self.mixed_audio = bytearray()
        self.participant_audio = defaultdict(bytearray)

    def handle(self, websocket):
        for message in websocket:
            message_type = int.from_bytes(message[:4], "little")
            if message_type == MESSAGE_TYPE_JSON:
                data = json.loads(message[4:])
                self.json_messages.append(data)
                print(f"[ws json] {json.dumps(data)[:400]}", flush=True)
                continue

            self.binary_counts[message_type] += 1
            if message_type == MESSAGE_TYPE_MIXED_AUDIO:
                self.mixed_audio += message[4:]
            elif message_type == MESSAGE_TYPE_PER_PARTICIPANT_AUDIO:
                id_length = message[4]
                participant_id = message[5 : 5 + id_length].decode("utf-8")
                self.participant_audio[participant_id] += message[5 + id_length :]

    def has_status_change(self, *changes):
        return any(message.get("type") == "MeetingStatusChange" and message.get("change") in changes for message in self.json_messages)

    def save_audio(self, out_dir):
        """Write the captured audio as wav files and return a summary of each."""
        os.makedirs(out_dir, exist_ok=True)
        recordings = {"mixed": self.mixed_audio, **{f"participant-{participant_id[:8]}": audio for participant_id, audio in self.participant_audio.items()}}
        summary = {}
        for name, raw_audio in recordings.items():
            samples = np.frombuffer(bytes(raw_audio), dtype=np.float32)
            if not samples.size:
                continue
            path = os.path.join(out_dir, f"{name}.wav")
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(AUDIO_SAMPLE_RATE)
                wav.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
            summary[name] = {"seconds": round(samples.size / AUDIO_SAMPLE_RATE, 1), "peak": round(float(np.abs(samples).max()), 3), "path": path}
        return summary


def serve_websocket_in_background(messages):
    server = serve(messages.handle, "localhost", 0, compression=None, max_size=None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.socket.getsockname()[1]


def start_page_server():
    # Loaded by path because importing it as part of the bots package would import Django
    spec = importlib.util.spec_from_file_location("static_page_server", STATIC_PAGE_SERVER_PATH)
    static_page_server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(static_page_server)
    return static_page_server.start_static_page_server(ADAPTER_DIR, PAGE_FILES)


def mint_guest_token(display_name):
    refresh_token = os.getenv("WEBEX_SERVICE_APP_REFRESH_TOKEN")
    client_id, client_secret = os.environ["WEBEX_CLIENT_ID"], os.environ["WEBEX_CLIENT_SECRET"]
    if refresh_token:
        tokens = refresh_service_app_tokens(client_id, client_secret, refresh_token)
    else:
        tokens = issue_service_app_tokens(os.environ["WEBEX_SERVICE_APP_ID"], client_id, client_secret, os.environ["WEBEX_TARGET_ORG_ID"], os.environ["WEBEX_PERSONAL_ACCESS_TOKEN"])
    return create_guest_token(tokens["access_token"], f"attendee-dev-bot-{int(time.time())}", display_name)


def start_chrome():
    options = webdriver.ChromeOptions()
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--use-fake-device-for-media-stream")
    options.add_argument("--use-fake-ui-for-media-stream")
    options.add_argument("--window-size=1280,800")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    # Selenium Manager downloads a chromedriver that matches the installed Chrome
    return webdriver.Chrome(options=options)


def inject_page_scripts(driver, websocket_port, webex_initial_data):
    """Inject what the real adapter injects: the shared payload, then this platform's payload."""
    scripts = [
        f"window.initialData = {json.dumps({'websocketPort': websocket_port})};",
        f"window.webexInitialData = {json.dumps(webex_initial_data)};",
    ]
    for path in (SHARED_PAYLOAD_PATH, PAYLOAD_PATH):
        with open(path) as payload:
            scripts.append(payload.read())
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": "\n".join(scripts)})


def wait_until(condition, timeout_seconds):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(1)
    return False


def sine_tone(seconds, frequency=440, volume=0.3):
    times = np.arange(int(AUDIO_SAMPLE_RATE * seconds)) / AUDIO_SAMPLE_RATE
    return (np.sin(2 * np.pi * frequency * times) * volume * 32767).astype(np.int16)


def make_bot_output(driver, args):
    if args.bot_image:
        with open(args.bot_image, "rb") as image:
            driver.execute_script("window.botOutputManager.displayImage(new Uint8Array(arguments[0]));", list(image.read()))
    if args.bot_audio_tone:
        time.sleep(BOT_OUTPUT_SETTLE_SECONDS)
        driver.execute_script("window.botOutputManager.playPCMAudio(new Int16Array(arguments[0]), arguments[1]);", sine_tone(seconds=3).tolist(), AUDIO_SAMPLE_RATE)


def check_page_loads(driver):
    """Smoke test: the SDK loaded and the page connected to the websocket server."""
    time.sleep(8)
    checks = {
        "webex_sdk_loaded": driver.execute_script("return typeof window.Webex?.init === 'function'"),
        "websocket_open": driver.execute_script("return window.ws.ws.readyState === WebSocket.OPEN"),
    }
    print(json.dumps(checks), flush=True)
    return all(checks.values())


def stay_in_meeting(driver, messages, args):
    """Join, then stay for args.duration seconds, or until the meeting is over."""

    def joined():
        return driver.execute_script("return window.webexJoinState.phase") == "joined"

    wait_until(lambda: joined() or messages.has_status_change("failed_to_join"), args.join_timeout)
    if not joined():
        print("Did not join the meeting", flush=True)
        return

    # The adapter does this once it is allowed to record
    driver.execute_script("window.ws.enableMediaSending();")
    print("Joined, starting the countdown", flush=True)
    make_bot_output(driver, args)

    def meeting_is_over(seconds):
        return wait_until(lambda: messages.has_status_change(*MEETING_OVER_CHANGES), seconds)

    # The screenshot is taken while the meeting is still going, so it shows the video the page rendered
    screenshot_delay = min(SCREENSHOT_DELAY_SECONDS, args.duration) if args.screenshot else 0
    if screenshot_delay and not meeting_is_over(screenshot_delay):
        driver.save_screenshot(args.screenshot)
    meeting_is_over(args.duration - screenshot_delay)


def leave_meeting(driver):
    """Leave properly, so the meeting does not keep the bot as a participant that is not in the meeting."""
    try:
        driver.execute_async_script("const done = arguments[arguments.length - 1]; window.leaveMeeting().then(done);")
    except WebDriverException as error:
        print(f"Could not leave the meeting: {error.msg}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--meeting-url", help="Meeting link or meeting number. Without it the page is only loaded.")
    parser.add_argument("--password", default="", help="Meeting password, only needed when joining by meeting number")
    parser.add_argument("--bot-name", default="Attendee Dev Bot")
    parser.add_argument("--duration", type=int, default=120, help="Seconds to stay in the meeting once joined")
    parser.add_argument("--join-timeout", type=int, default=900, help="Seconds to wait to be admitted")
    parser.add_argument("--bot-image", help="Image the bot shows as its webcam once joined")
    parser.add_argument("--bot-audio-tone", action="store_true", help="Make the bot play a 3 second tone once joined")
    parser.add_argument("--screenshot", help="Save a screenshot of the page to this path before leaving")
    parser.add_argument("--audio-out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"), help="Where to write captured audio")
    args = parser.parse_args()

    load_dotenv(os.path.join(REPO_ROOT, ".env"))
    # Turn a terminate request into a normal exit, so the bot still leaves the meeting
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))

    messages = BotMessages()
    websocket_port = serve_websocket_in_background(messages)
    http_port = start_page_server()

    webex_initial_data = {"meetingUrl": args.meeting_url, "meetingPassword": args.password, "botName": args.bot_name}
    if args.meeting_url:
        webex_initial_data["guestToken"] = mint_guest_token(args.bot_name)

    driver = start_chrome()
    try:
        inject_page_scripts(driver, websocket_port, webex_initial_data)
        driver.get(f"http://127.0.0.1:{http_port}/webex_chromedriver_page.html")

        if not args.meeting_url:
            sys.exit(0 if check_page_loads(driver) else 1)
        stay_in_meeting(driver, messages, args)
    finally:
        if args.meeting_url:
            leave_meeting(driver)
        print(f"Binary messages by type: {dict(messages.binary_counts)}", flush=True)
        print(f"Saved audio: {json.dumps(messages.save_audio(args.audio_out_dir), indent=1)}", flush=True)
        driver.quit()


if __name__ == "__main__":
    main()
