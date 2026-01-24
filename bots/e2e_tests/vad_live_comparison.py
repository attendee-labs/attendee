#!/usr/bin/env python3
"""
VAD Live Comparison E2E Test

This script tests VAD performance using real meeting bots. Run this test twice:
1. First with your server configured with VAD_PROVIDER=webrtc
2. Then with your server configured with VAD_PROVIDER=silero

The results are saved to JSON files for comparison.

Usage:
    # Run test with two speakers (like diarization test):
    python vad_live_comparison.py \\
        --api-key YOUR_API_KEY \\
        --base-url https://your-server.com \\
        --meeting-url "https://meet.google.com/xxx" \\
        --speaker1 /path/to/speaker1.mp3 \\
        --speaker2 /path/to/speaker2.mp3 \\
        --vad-label webrtc \\
        --output-dir ./vad_results \\
        --verbose

    # Or with a single speaker:
    python vad_live_comparison.py \\
        --api-key YOUR_API_KEY \\
        --base-url https://your-server.com \\
        --meeting-url "https://meet.google.com/xxx" \\
        --speaker1 /path/to/audio.mp3 \\
        --vad-label webrtc \\
        --output-dir ./vad_results \\
        --verbose

    # Then change server to VAD_PROVIDER=silero and run again:
    python vad_live_comparison.py \\
        --api-key YOUR_API_KEY \\
        --base-url https://your-server.com \\
        --meeting-url "https://meet.google.com/xxx" \\
        --speaker1 /path/to/speaker1.mp3 \\
        --speaker2 /path/to/speaker2.mp3 \\
        --vad-label silero \\
        --output-dir ./vad_results \\
        --verbose

    # Compare results:
    python vad_live_comparison.py --compare ./vad_results

The test:
1. Creates speaker bot(s) that play the test audio into the meeting
2. Creates a recorder bot that records and transcribes
3. Waits for bots to finish
4. Saves utterance data (count, timing, transcriptions) to JSON
5. Optionally compares results from different VAD providers
"""

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests

# ----------------------------
# Data classes
# ----------------------------


@dataclass
class UtteranceData:
    """Data about a single utterance."""
    speaker_name: str
    start_timestamp_ms: Optional[int]
    end_timestamp_ms: Optional[int]
    duration_ms: Optional[int]
    transcript: str
    word_count: int


@dataclass
class VADTestResult:
    """Complete result from a VAD test run."""
    vad_label: str
    timestamp: str
    audio_file: str
    meeting_url: str
    speaker_bot_id: str
    recorder_bot_id: str
    total_utterances: int
    total_words: int
    total_speech_duration_ms: int
    utterances: List[Dict]
    participant_events: List[Dict]
    raw_transcript: List[Dict]


# ----------------------------
# API Client
# ----------------------------


class AttendeeClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {api_key}",
                "Content-Type": "application/json",
            }
        )
        self.timeout = timeout

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def create_bot(
        self,
        meeting_url: str,
        bot_name: str,
        enable_transcription: bool = False,
        extra: Optional[Dict] = None,
    ) -> Dict:
        payload = {"meeting_url": meeting_url, "bot_name": bot_name}
        if enable_transcription:
            # Use OpenAI for transcription (faster and cheaper)
            payload["transcription_settings"] = {
                "openai": {
                    "model": "gpt-4o-transcribe",
                    "language": "en",
                }
            }
            payload["recording_settings"] = {
                "format": "mp3",
                "record_async_transcription_audio_chunks": True,
            }
        if extra:
            payload.update(extra)
        r = self.session.post(
            self._url("/api/v1/bots"),
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def get_bot(self, bot_id: str) -> Dict:
        r = self.session.get(self._url(f"/api/v1/bots/{bot_id}"), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def tell_bot_to_leave(self, bot_id: str) -> None:
        try:
            r = self.session.post(
                self._url(f"/api/v1/bots/{bot_id}/leave"), timeout=self.timeout
            )
            if r.status_code in (200, 202, 204):
                return
        except requests.RequestException:
            pass

        try:
            r = self.session.delete(
                self._url(f"/api/v1/bots/{bot_id}"), timeout=self.timeout
            )
        except requests.RequestException:
            pass

    def output_audio(self, bot_id: str, audio_path: Path) -> None:
        b64_data = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        
        # Determine content type from extension
        ext = audio_path.suffix.lower()
        content_type = {
            ".mp3": "audio/mp3",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
        }.get(ext, "audio/mp3")
        
        json_payload = {"type": content_type, "data": b64_data}
        url = self._url(f"/api/v1/bots/{bot_id}/output_audio")

        r = self.session.post(url, data=json.dumps(json_payload), timeout=self.timeout)
        r.raise_for_status()

    def get_transcript(self, bot_id: str) -> List[Dict]:
        r = self.session.get(
            self._url(f"/api/v1/bots/{bot_id}/transcript"), timeout=self.timeout
        )
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return []

    def get_participant_events(self, bot_id: str) -> Dict:
        r = self.session.get(
            self._url(f"/api/v1/bots/{bot_id}/participant_events"), timeout=self.timeout
        )
        r.raise_for_status()
        return r.json()


# ----------------------------
# Helpers
# ----------------------------


def state_is_joined_recording(state: str) -> bool:
    s = (state or "").strip().lower()
    return "joined" in s and "record" in s


def state_is_ended(state: str) -> bool:
    return (state or "").strip().lower() == "ended"


def wait_for_state(
    client: AttendeeClient,
    bot_id: str,
    predicate,
    desc: str,
    timeout_s: int,
    poll_s: float = 2.0,
    verbose: bool = False,
) -> Dict:
    start = time.time()
    last_state = ""
    while True:
        bot = client.get_bot(bot_id)
        state = str(bot.get("state", ""))
        if state != last_state and verbose:
            print(f"  Bot {bot_id}: {state}")
            last_state = state
        if predicate(state):
            return bot
        if (time.time() - start) > timeout_s:
            raise TimeoutError(
                f"Timed out waiting for state '{desc}'. Last state={state!r}"
            )
        time.sleep(poll_s)


def get_audio_duration_ms(audio_path: Path) -> int:
    """Get duration of audio file in milliseconds."""
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(str(audio_path))
        return len(audio)
    except ImportError:
        print("WARNING: pydub not installed, cannot determine audio duration")
        return 60000  # Default 60 seconds


# ----------------------------
# Test execution
# ----------------------------


def run_vad_test(
    client: AttendeeClient,
    meeting_url: str,
    audio_files: List[Path],
    vad_label: str,
    verbose: bool = False,
    join_timeout: int = 180,
    leave_after: Optional[float] = None,
    end_timeout: int = 300,
) -> VADTestResult:
    """
    Run a VAD test by creating bots, playing audio, and collecting results.
    
    Args:
        audio_files: List of audio files. Each gets its own speaker bot.
    """
    num_speakers = len(audio_files)
    speaker_names = [f"Speaker {i+1} ({vad_label})" for i in range(num_speakers)]
    recorder_name = f"Recorder ({vad_label})"
    
    # Get max audio duration to know how long to wait
    max_duration_ms = max(get_audio_duration_ms(p) for p in audio_files)
    if leave_after is None:
        # Wait for longest audio to finish plus 10 seconds buffer
        leave_after = (max_duration_ms / 1000) + 10
    
    if verbose:
        print(f"Max audio duration: {max_duration_ms / 1000:.1f}s")
        print(f"Will leave after: {leave_after:.1f}s")
    
    # 1) Create bots
    if verbose:
        print("\nCreating bots...")
    
    speaker_bots = []
    for i, (name, audio_path) in enumerate(zip(speaker_names, audio_files)):
        bot = client.create_bot(
            meeting_url=meeting_url,
            bot_name=name,
            enable_transcription=False,
        )
        speaker_bots.append((bot["id"], name, audio_path))
        if verbose:
            print(f"  {name}: {bot['id']}")
    
    recorder_bot = client.create_bot(
        meeting_url=meeting_url,
        bot_name=recorder_name,
        enable_transcription=True,
    )
    recorder_id = recorder_bot["id"]
    
    if verbose:
        print(f"  {recorder_name}: {recorder_id}")
    
    all_bot_ids = [bot_id for bot_id, _, _ in speaker_bots] + [recorder_id]
    
    try:
        # 2) Wait for all bots to join
        if verbose:
            print("\nWaiting for bots to join...")
        
        for bot_id, name, _ in speaker_bots:
            wait_for_state(
                client, bot_id, state_is_joined_recording,
                "joined_recording", join_timeout, verbose=verbose
            )
        wait_for_state(
            client, recorder_id, state_is_joined_recording,
            "joined_recording", join_timeout, verbose=verbose
        )
        
        # Small delay to ensure everything is ready
        time.sleep(2)
        
        # 3) Play audio from all speakers concurrently
        if verbose:
            print(f"\nPlaying audio from {num_speakers} speaker(s)...")
        
        import concurrent.futures
        
        def play_audio(bot_id: str, audio_path: Path):
            client.output_audio(bot_id, audio_path)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_speakers) as pool:
            futures = []
            for bot_id, name, audio_path in speaker_bots:
                if verbose:
                    print(f"  {name}: {audio_path.name}")
                futures.append(pool.submit(play_audio, bot_id, audio_path))
            
            # Wait for all to complete and check for errors
            errors = []
            for fut in futures:
                try:
                    fut.result()
                except Exception as e:
                    errors.append(e)
            if errors:
                raise RuntimeError(f"Failed to play audio: {errors}")
        
        # 4) Wait for audio to finish
        if verbose:
            print(f"\nWaiting {leave_after:.1f}s for audio to complete...")
        
        time.sleep(leave_after)
        
        # 5) Tell bots to leave
        if verbose:
            print("\nTelling bots to leave...")
        
        for bot_id in all_bot_ids:
            client.tell_bot_to_leave(bot_id)
        
        # 6) Wait for bots to end
        if verbose:
            print("\nWaiting for bots to end...")
        
        for bot_id in all_bot_ids:
            wait_for_state(
                client, bot_id, state_is_ended,
                "ended", end_timeout, verbose=verbose
            )
        
        # 7) Collect results
        if verbose:
            print("\nCollecting results...")
        
        transcript = client.get_transcript(recorder_id)
        participant_events = client.get_participant_events(recorder_id)
        
        # Process utterances
        utterances = []
        total_words = 0
        total_speech_duration = 0
        
        for utt in transcript:
            transcription = utt.get("transcription", {})
            transcript_text = transcription.get("transcript", "")
            words = transcript_text.split()
            
            # Try to get timing info
            start_ts = utt.get("start_timestamp_ms")
            end_ts = utt.get("end_timestamp_ms")
            duration = None
            if start_ts is not None and end_ts is not None:
                duration = end_ts - start_ts
                total_speech_duration += duration
            
            utterance_data = {
                "speaker_name": utt.get("speaker_name", ""),
                "start_timestamp_ms": start_ts,
                "end_timestamp_ms": end_ts,
                "duration_ms": duration,
                "transcript": transcript_text,
                "word_count": len(words),
            }
            utterances.append(utterance_data)
            total_words += len(words)
        
        # Filter to only speech events from participant events
        events = participant_events.get("results", [])
        speech_events = [
            e for e in events
            if e.get("event_type") in ("speech_start", "speech_stop")
        ]
        
        result = VADTestResult(
            vad_label=vad_label,
            timestamp=datetime.now().isoformat(),
            audio_file=", ".join(str(p) for p in audio_files),
            meeting_url=meeting_url,
            speaker_bot_id=", ".join(bot_id for bot_id, _, _ in speaker_bots),
            recorder_bot_id=recorder_id,
            total_utterances=len(utterances),
            total_words=total_words,
            total_speech_duration_ms=total_speech_duration,
            utterances=utterances,
            participant_events=speech_events,
            raw_transcript=transcript,
        )
        
        return result
        
    except Exception as e:
        # Clean up on error
        if verbose:
            print(f"\nError occurred: {e}")
            print("Cleaning up bots...")
        
        for bot_id in all_bot_ids:
            try:
                client.tell_bot_to_leave(bot_id)
            except Exception:
                pass
        
        raise


def save_result(result: VADTestResult, output_dir: Path):
    """Save test result to JSON file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    filename = f"vad_result_{result.vad_label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = output_dir / filename
    
    with open(filepath, "w") as f:
        json.dump(asdict(result), f, indent=2)
    
    print(f"Results saved to: {filepath}")
    return filepath


def load_results(output_dir: Path) -> List[VADTestResult]:
    """Load all results from output directory."""
    results = []
    for filepath in output_dir.glob("vad_result_*.json"):
        with open(filepath) as f:
            data = json.load(f)
            # Convert back to dataclass (simplified)
            results.append(data)
    return results


def compare_results(output_dir: Path):
    """Compare VAD results from different test runs."""
    results = load_results(output_dir)
    
    if len(results) < 2:
        print("ERROR: Need at least 2 result files to compare")
        print(f"Found {len(results)} result(s) in {output_dir}")
        return
    
    # Group by VAD label
    by_label = {}
    for r in results:
        label = r["vad_label"]
        if label not in by_label:
            by_label[label] = []
        by_label[label].append(r)
    
    print("\n" + "=" * 70)
    print("VAD LIVE COMPARISON REPORT")
    print("=" * 70)
    
    print(f"\nFound {len(results)} test results across {len(by_label)} VAD configurations:")
    for label, runs in by_label.items():
        print(f"  - {label}: {len(runs)} run(s)")
    
    # Compare the most recent run from each VAD type
    print("\n" + "-" * 70)
    print("Comparing most recent run from each VAD type:")
    print("-" * 70)
    
    latest = {}
    for label, runs in by_label.items():
        # Sort by timestamp and get most recent
        runs.sort(key=lambda x: x["timestamp"], reverse=True)
        latest[label] = runs[0]
    
    if len(latest) < 2:
        print("Need results from at least 2 different VAD types to compare")
        return
    
    # Print comparison table
    labels = sorted(latest.keys())
    
    header = f"{'Metric':<35}"
    for label in labels:
        header += f" {label:>15}"
    print(header)
    print("-" * 70)
    
    # Utterance count
    row = f"{'Total Utterances':<35}"
    for label in labels:
        row += f" {latest[label]['total_utterances']:>15}"
    print(row)
    
    # Word count
    row = f"{'Total Words Transcribed':<35}"
    for label in labels:
        row += f" {latest[label]['total_words']:>15}"
    print(row)
    
    # Speech duration
    row = f"{'Total Speech Duration (ms)':<35}"
    for label in labels:
        row += f" {latest[label]['total_speech_duration_ms']:>15}"
    print(row)
    
    # Speech events
    row = f"{'Speech Events (start/stop)':<35}"
    for label in labels:
        row += f" {len(latest[label]['participant_events']):>15}"
    print(row)
    
    print("-" * 70)
    
    # Detailed utterance comparison
    print("\nUtterance Details:")
    
    for label in labels:
        result = latest[label]
        print(f"\n  {label.upper()} ({result['timestamp'][:10]}):")
        
        if result["utterances"]:
            for i, utt in enumerate(result["utterances"][:10]):  # Limit to first 10
                duration = utt.get("duration_ms", "?")
                transcript = utt.get("transcript", "")[:50]
                if len(utt.get("transcript", "")) > 50:
                    transcript += "..."
                print(f"    {i+1}. [{duration}ms] {transcript}")
            
            if len(result["utterances"]) > 10:
                print(f"    ... and {len(result['utterances']) - 10} more")
        else:
            print("    (no utterances)")
    
    print("\n" + "=" * 70)


def print_result_summary(result: VADTestResult):
    """Print a summary of the test result."""
    print("\n" + "=" * 70)
    print(f"VAD TEST RESULT: {result.vad_label.upper()}")
    print("=" * 70)
    print(f"Timestamp: {result.timestamp}")
    print(f"Audio: {Path(result.audio_file).name}")
    print(f"Meeting: {result.meeting_url}")
    print("-" * 70)
    print(f"Total Utterances: {result.total_utterances}")
    print(f"Total Words: {result.total_words}")
    print(f"Total Speech Duration: {result.total_speech_duration_ms}ms")
    print(f"Speech Events: {len(result.participant_events)}")
    
    if result.utterances:
        print("\nUtterances:")
        for i, utt in enumerate(result.utterances[:5]):
            duration = utt.get("duration_ms", "?")
            transcript = utt.get("transcript", "")[:60]
            if len(utt.get("transcript", "")) > 60:
                transcript += "..."
            print(f"  {i+1}. [{duration}ms] {transcript}")
        if len(result.utterances) > 5:
            print(f"  ... and {len(result.utterances) - 5} more")
    
    print("=" * 70)


# ----------------------------
# Main
# ----------------------------


def main():
    parser = argparse.ArgumentParser(
        description="VAD Live Comparison E2E Test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    
    # Test execution arguments
    parser.add_argument("--api-key", help="Attendee API key")
    parser.add_argument("--base-url", help="Attendee base URL")
    parser.add_argument("--meeting-url", help="Meeting URL to join")
    parser.add_argument("--speaker1", type=Path, help="Path to first speaker audio (mp3/wav)")
    parser.add_argument("--speaker2", type=Path, help="Path to second speaker audio (mp3/wav)")
    parser.add_argument(
        "--vad-label",
        default="unknown",
        help="Label for this VAD configuration (e.g., 'webrtc' or 'silero')",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./vad_results"),
        help="Directory to save results (default: ./vad_results)",
    )
    parser.add_argument(
        "--join-timeout",
        type=int,
        default=180,
        help="Seconds to wait for bots to join (default: 180)",
    )
    parser.add_argument(
        "--leave-after",
        type=float,
        default=None,
        help="Seconds to wait after playing audio before leaving (default: auto)",
    )
    parser.add_argument(
        "--end-timeout",
        type=int,
        default=300,
        help="Seconds to wait for bots to end (default: 300)",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    
    # Comparison mode
    parser.add_argument(
        "--compare",
        type=Path,
        metavar="DIR",
        help="Compare results in the specified directory instead of running a test",
    )
    
    args = parser.parse_args()
    
    # Comparison mode
    if args.compare:
        compare_results(args.compare)
        return
    
    # Validation for test mode
    if not args.api_key:
        print("ERROR: --api-key is required", file=sys.stderr)
        sys.exit(2)
    if not args.base_url:
        print("ERROR: --base-url is required", file=sys.stderr)
        sys.exit(2)
    if not args.meeting_url:
        print("ERROR: --meeting-url is required", file=sys.stderr)
        sys.exit(2)
    if not args.speaker1:
        print("ERROR: --speaker1 is required", file=sys.stderr)
        sys.exit(2)
    if not args.speaker1.exists():
        print(f"ERROR: Speaker 1 audio file not found: {args.speaker1}", file=sys.stderr)
        sys.exit(1)
    if args.speaker2 and not args.speaker2.exists():
        print(f"ERROR: Speaker 2 audio file not found: {args.speaker2}", file=sys.stderr)
        sys.exit(1)
    
    audio_files = [args.speaker1]
    if args.speaker2:
        audio_files.append(args.speaker2)
    
    print(f"Running VAD test with label: {args.vad_label}")
    print(f"Server: {args.base_url}")
    print(f"Audio files: {[str(f) for f in audio_files]}")
    
    client = AttendeeClient(args.base_url, args.api_key)
    
    result = run_vad_test(
        client=client,
        meeting_url=args.meeting_url,
        audio_files=audio_files,
        vad_label=args.vad_label,
        verbose=args.verbose,
        join_timeout=args.join_timeout,
        leave_after=args.leave_after,
        end_timeout=args.end_timeout,
    )
    
    # Print summary
    print_result_summary(result)
    
    # Save result
    save_result(result, args.output_dir)
    
    print("\nTo compare with another VAD configuration:")
    print(f"  1. Change your server's VAD_PROVIDER environment variable")
    print(f"  2. Run this test again with a different --vad-label")
    print(f"  3. Run: python {Path(__file__).name} --compare {args.output_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
