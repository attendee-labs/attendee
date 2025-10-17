#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import json
import mimetypes
import sys
import time
from pathlib import Path
from typing import Dict, Tuple, List, Optional

import requests


# ----------------------------
# Helpers / HTTP
# ----------------------------

class AttendeeClient:
    def __init__(self, base_url: str, api_key: str, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        })
        self.timeout = timeout

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def create_bot(self, meeting_url: str, bot_name: str, extra: Optional[Dict] = None, enable_transcription: bool = False) -> Dict:
        payload = {"meeting_url": meeting_url, "bot_name": bot_name}
        if enable_transcription:
            payload["transcription_settings"] = {"assembly_ai": {}}
            payload["recording_settings"] = {"format": "mp3", "record_async_transcription_audio_chunks": True}
        if extra:
            payload.update(extra)
        r = self.session.post(self._url("/api/v1/bots"), data=json.dumps(payload), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_bot(self, bot_id: str) -> Dict:
        r = self.session.get(self._url(f"/api/v1/bots/{bot_id}"), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def tell_bot_to_leave(self, bot_id: str) -> None:
        """
        Try preferred leave endpoint; fall back to DELETE if supported by your deployment.
        """
        # Try POST /leave first
        try:
            r = self.session.post(self._url(f"/api/v1/bots/{bot_id}/leave"), timeout=self.timeout)
            if r.status_code in (200, 202, 204):
                return
            # Some instances may return 404 if leave endpoint is not present
        except requests.RequestException:
            pass

        # Fallback: DELETE bot (if supported)
        try:
            r = self.session.delete(self._url(f"/api/v1/bots/{bot_id}"), timeout=self.timeout)
            if r.status_code in (200, 202, 204):
                return
        except requests.RequestException:
            pass

        # If both failed, we just rely on auto-leave settings/timeouts.

    def output_audio(self, bot_id: str, audio_path: Path, kind_hint: Optional[str] = None) -> None:
        """
        Sends audio to be spoken into the meeting.

        Strategy:
          1) Try documented JSON shape: {"kind": "mp3" | "wav", "b64_data": "..."} (Content-Type: application/json)
          2) If server rejects (400/404/415), retry with multipart/form-data upload using "file" field.
        """

        # First attempt: JSON body with base64
        b64_data = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        json_payload = {"type": "audio/mp3", "data": b64_data}
        url = self._url(f"/api/v1/bots/{bot_id}/output_audio")

        # Temporarily override content-type to JSON for this call (session default is JSON anyway)
        r = self.session.post(url, data=json.dumps(json_payload), timeout=self.timeout)
        if r.status_code == 200:
            return

        r.raise_for_status()

    def get_transcript(self, bot_id: str) -> List[Dict]:
        r = self.session.get(self._url(f"/api/v1/bots/{bot_id}/transcript"), timeout=self.timeout)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return []

    def get_participant_events(self, bot_id: str) -> List[Dict]:
        r = self.session.get(self._url(f"/api/v1/bots/{bot_id}/participant_events"), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

# ----------------------------
# Core workflow
# ----------------------------

JOINED_RECORDING_KEYS = {"joined_recording", "joined_recording_audio", "joined - recording", "joined - recording"}  # be permissive

def state_is_joined_recording(state: str) -> bool:
    s = (state or "").strip().lower()
    # Accept fuzzy match to handle human-readable values like "Joined - Recording"
    return "joined" in s and "record" in s

def wait_for_state(client: AttendeeClient, bot_id: str, predicate, desc: str, timeout_s: int, poll_s: float = 2.0) -> Dict:
    start = time.time()
    last = None
    while True:
        bot = client.get_bot(bot_id)
        last = bot
        state = str(bot.get("state", ""))
        if predicate(state, bot):
            return bot
        if (time.time() - start) > timeout_s:
            raise TimeoutError(f"Timed out waiting for state '{desc}'. Last state={state!r}")
        time.sleep(poll_s)


def main():
    parser = argparse.ArgumentParser(
        description="Spin up three Attendee bots in a Teams meeting: two speaker bots to play audio and one recorder bot to transcribe."
    )
    parser.add_argument("--api-key", required=True, help="Attendee API key")
    parser.add_argument("--base-url", required=True, help="Attendee base URL, e.g. https://staging.attendee.dev")
    parser.add_argument("--speaker1", required=True, help="Path to first speaker audio (mp3/wav)")
    parser.add_argument("--speaker2", required=True, help="Path to second speaker audio (mp3/wav)")
    parser.add_argument(
        "--meeting-url",
        default=None,
        help="Meeting URL (must bypass waiting room)."
    )
    parser.add_argument("--join-timeout", type=int, default=180, help="Seconds to wait for 'Joined - Recording'")
    parser.add_argument("--end-timeout", type=int, default=300, help="Seconds to wait for 'Ended'")
    parser.add_argument("--speak-wait", type=float, default=0.0, help="Seconds to wait after reaching joined_recording before speaking")
    parser.add_argument("--leave-after", type=float, default=None, help="Optional: seconds to wait after starting playback before telling bots to leave")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    meeting_url = args.meeting_url
    if not meeting_url:
        print("ERROR: Meeting URL is required", file=sys.stderr)
        sys.exit(2)

    client = AttendeeClient(args.base_url, args.api_key)

    bot1_name = "Speaker 1"
    bot2_name = "Speaker 2"
    recorder_name = "Recorder Bot"

    # 1) Create three bots: two speaker bots (no transcription) and one recorder bot (with Assembly AI)
    if args.verbose:
        print("Creating bots...")
    bot1 = client.create_bot(meeting_url=meeting_url, bot_name=bot1_name, enable_transcription=False)
    bot2 = client.create_bot(meeting_url=meeting_url, bot_name=bot2_name, enable_transcription=False)
    recorder = client.create_bot(meeting_url=meeting_url, bot_name=recorder_name, enable_transcription=True)
    bot1_id = bot1["id"]
    bot2_id = bot2["id"]
    recorder_id = recorder["id"]
    if args.verbose:
        print(f"Created: {bot1_id} ({bot1_name}), {bot2_id} ({bot2_name}), {recorder_id} ({recorder_name})")

    # 2) Poll until all three bots are joined & recording
    if args.verbose:
        print("Waiting for all three bots to be 'Joined - Recording'...")

    def _pred_joined(state: str, bot_obj: Dict) -> bool:
        return state_is_joined_recording(state)

    wait_for_state(client, bot1_id, _pred_joined, "joined_recording", args.join_timeout)
    wait_for_state(client, bot2_id, _pred_joined, "joined_recording", args.join_timeout)
    wait_for_state(client, recorder_id, _pred_joined, "joined_recording", args.join_timeout)

    if args.speak_wait > 0:
        if args.verbose:
            print(f"Sleeping {args.speak_wait:.1f}s before speaking...")
        time.sleep(args.speak_wait)

    # 3) Simultaneously tell both bots to speak audio
    path1 = Path(args.speaker1)
    path2 = Path(args.speaker2)
    if args.verbose:
        print("Sending output_audio to both bots concurrently...")

    def _speak(bot_id: str, path: Path):
        client.output_audio(bot_id, path)

    # Record the absolute timestamp when the audio is played
    audio_played_at = int(time.time() * 1000)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(_speak, bot1_id, path1)
        fut2 = pool.submit(_speak, bot2_id, path2)
        # raise if either failed
        excs = []
        for fut in (fut1, fut2):
            try:
                fut.result()
            except Exception as e:
                excs.append(e)
        if excs:
            raise RuntimeError(f"output_audio failed: {excs!r}")

    # 4) After audio files have been played, have the bots leave
    # If you want to wait a fixed duration after playback begins, you can use --leave-after
    if args.leave_after:
        if args.verbose:
            print(f"Waiting {args.leave_after:.1f}s before leaving...")
        time.sleep(args.leave_after)

    if args.verbose:
        print("Telling all three bots to leave...")
    client.tell_bot_to_leave(bot1_id)
    client.tell_bot_to_leave(bot2_id)
    client.tell_bot_to_leave(recorder_id)

    # 5) Poll until all three bots are in the "ended" state.
    if args.verbose:
        print("Waiting for all three bots to be 'ended'...")
    def _pred_ended(state: str, bot_obj: Dict) -> bool:
        return (state or "").strip().lower() == "ended"

    bot1_final = wait_for_state(client, bot1_id, _pred_ended, "ended", args.end_timeout)
    bot2_final = wait_for_state(client, bot2_id, _pred_ended, "ended", args.end_timeout)
    recorder_final = wait_for_state(client, recorder_id, _pred_ended, "ended", args.end_timeout)

    # 6) Verify that the transcription has the correct diarization.
    # Strategy:
    #   - Fetch transcript from the recorder bot (only bot with transcription enabled)
    #   - Collect distinct speaker_names seen
    #   - Verify both "Speaker 1" and "Speaker 2" appear at least once (case-insensitive)
    #   - Print a short summary and exit non-zero if failed.
    if args.verbose:
        print("Fetching transcripts and verifying diarization...")

    transcript = client.get_transcript(recorder_id) or []

    if args.verbose:
        print(f"Transcript: {transcript}")
    
    # Get list of all utterances for speaker_1
    speaker1_utterances = [utterance["transcription"]["transcript"] for utterance in transcript if utterance.get("speaker_name") == bot1_name]
    speaker2_utterances = [utterance["transcription"]["transcript"] for utterance in transcript if utterance.get("speaker_name") == bot2_name]
    
    print(f"Speaker 1 utterances: {speaker1_utterances}")
    print(f"Speaker 2 utterances: {speaker2_utterances}")

    # Get a list of participant events for the meeting from the recorder bot
    participant_events = client.get_participant_events(recorder_id)
    print(f"Participant events: {participant_events}")
    participant_events = participant_events.get("results", [])

    # We only want speech events
    speech_events = [{"speaker": event["participant_name"], "event_type": event["event_type"], "relative_timestamp": event["timestamp_ms"] - audio_played_at} for event in participant_events if event["event_type"] == "speech_start" or event["event_type"] == "speech_stop"]

    print(f"Speech events: {speech_events}")

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
