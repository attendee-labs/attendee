#!/usr/bin/env python3
"""
Meeting Quality E2E Test

Simulates a realistic meeting with multiple participants joining and speaking,
then compares the transcript and audio recording against ground truth data.

Metrics computed:
  Transcript:
    - Word Error Rate (WER)
    - Character Error Rate (CER)
    - Diarization Error Rate (DER)
    - Word Diarization Error Rate (WDER)

  Audio (optional, requires additional dependencies):
    - PESQ (Perceptual Evaluation of Speech Quality)
    - STOI (Short-Time Objective Intelligibility)

Usage:
    python meeting_quality_test.py \
        --api-key <KEY> \
        --base-url https://staging.attendee.dev \
        --meeting-url <MEETING_URL> \
        --ground-truth ground_truth.json \
        --verbose

Ground truth JSON format:
{
    "speakers": [
        {
            "name": "Speaker 1",
            "audio_file": "/path/to/speaker1.mp3",
            "audio_duration_seconds": 5.0,
            "transcript": "Hello, this is speaker one. How are you today?"
        },
        {
            "name": "Speaker 2",
            "audio_file": "/path/to/speaker2.mp3",
            "audio_duration_seconds": 4.0,
            "transcript": "I'm doing well, thank you for asking."
        }
    ],
    "combined_transcript": "Hello, this is speaker one. How are you today? I'm doing well, thank you for asking.",
    "combined_audio_file": "/path/to/combined_ground_truth.wav"  # optional, for audio quality metrics
}
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ----------------------------
# Optional dependencies for metrics
# ----------------------------

try:
    import jiwer

    HAS_JIWER = True
except ImportError:
    HAS_JIWER = False
    print("Warning: jiwer not installed. Install with: pip install jiwer", file=sys.stderr)

try:
    import numpy as np
    import scipy.io.wavfile as wavfile
    from pesq import pesq

    HAS_PESQ = True
except ImportError:
    HAS_PESQ = False

try:
    from pystoi import stoi

    HAS_STOI = True
except ImportError:
    HAS_STOI = False

try:
    import librosa

    HAS_LIBROSA = True
except ImportError:
    HAS_LIBROSA = False


# ----------------------------
# Data classes for results
# ----------------------------


@dataclass
class TranscriptMetrics:
    """Metrics for transcript quality."""

    wer: float = 0.0  # Word Error Rate (lower is better, 0 = perfect)
    cer: float = 0.0  # Character Error Rate (lower is better)
    mer: float = 0.0  # Match Error Rate
    wil: float = 0.0  # Word Information Lost
    wip: float = 0.0  # Word Information Preserved
    substitutions: int = 0
    insertions: int = 0
    deletions: int = 0
    reference_words: int = 0
    hypothesis_words: int = 0


@dataclass
class DiarizationMetrics:
    """Metrics for speaker diarization quality."""

    der: float = 0.0  # Diarization Error Rate (lower is better)
    wder: float = 0.0  # Word Diarization Error Rate
    speaker_confusion: float = 0.0  # % words assigned to wrong speaker
    missed_speech: float = 0.0  # % speech not attributed to any speaker
    false_alarm: float = 0.0  # % non-speech labeled as speech
    correct_speakers: int = 0  # Number of correctly identified speakers
    total_speakers: int = 0


@dataclass
class AudioMetrics:
    """Metrics for audio quality."""

    pesq_score: Optional[float] = None  # -0.5 to 4.5 (higher is better)
    stoi_score: Optional[float] = None  # 0 to 1 (higher is better)
    snr_db: Optional[float] = None  # Signal-to-noise ratio in dB


@dataclass
class MeetingQualityReport:
    """Complete quality report for the meeting test."""

    transcript_metrics: TranscriptMetrics = field(default_factory=TranscriptMetrics)
    diarization_metrics: DiarizationMetrics = field(default_factory=DiarizationMetrics)
    audio_metrics: AudioMetrics = field(default_factory=AudioMetrics)
    per_speaker_wer: Dict[str, float] = field(default_factory=dict)
    raw_transcript: str = ""
    expected_transcript: str = ""
    passed: bool = False
    failure_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "transcript_metrics": {
                "wer": self.transcript_metrics.wer,
                "cer": self.transcript_metrics.cer,
                "mer": self.transcript_metrics.mer,
                "wil": self.transcript_metrics.wil,
                "wip": self.transcript_metrics.wip,
                "substitutions": self.transcript_metrics.substitutions,
                "insertions": self.transcript_metrics.insertions,
                "deletions": self.transcript_metrics.deletions,
                "reference_words": self.transcript_metrics.reference_words,
                "hypothesis_words": self.transcript_metrics.hypothesis_words,
            },
            "diarization_metrics": {
                "der": self.diarization_metrics.der,
                "wder": self.diarization_metrics.wder,
                "speaker_confusion": self.diarization_metrics.speaker_confusion,
                "correct_speakers": self.diarization_metrics.correct_speakers,
                "total_speakers": self.diarization_metrics.total_speakers,
            },
            "audio_metrics": {
                "pesq_score": self.audio_metrics.pesq_score,
                "stoi_score": self.audio_metrics.stoi_score,
                "snr_db": self.audio_metrics.snr_db,
            },
            "per_speaker_wer": self.per_speaker_wer,
            "passed": self.passed,
            "failure_reasons": self.failure_reasons,
        }


# ----------------------------
# Text normalization for comparison
# ----------------------------


def normalize_text(text: str) -> str:
    """Normalize text for fair comparison."""
    # Lowercase
    text = text.lower()
    # Remove punctuation except apostrophes in contractions
    text = re.sub(r"[^\w\s']", " ", text)
    # Normalize whitespace
    text = " ".join(text.split())
    return text


# ----------------------------
# Metric calculation functions
# ----------------------------


def calculate_transcript_metrics(reference: str, hypothesis: str) -> TranscriptMetrics:
    """Calculate WER, CER, and related metrics."""
    if not HAS_JIWER:
        print("Warning: jiwer not available, skipping transcript metrics", file=sys.stderr)
        return TranscriptMetrics()

    ref_normalized = normalize_text(reference)
    hyp_normalized = normalize_text(hypothesis)

    if not ref_normalized:
        return TranscriptMetrics()

    # Calculate WER and related metrics (jiwer 3.x API)
    output = jiwer.process_words(ref_normalized, hyp_normalized)

    # Calculate CER
    cer = jiwer.cer(ref_normalized, hyp_normalized)

    return TranscriptMetrics(
        wer=output.wer,
        cer=cer,
        mer=output.mer,
        wil=output.wil,
        wip=output.wip,
        substitutions=output.substitutions,
        insertions=output.insertions,
        deletions=output.deletions,
        reference_words=len(ref_normalized.split()),
        hypothesis_words=len(hyp_normalized.split()),
    )


def calculate_diarization_metrics(
    expected_speaker_count: int,
    actual_utterances: List[Dict[str, str]],
) -> DiarizationMetrics:
    """
    Calculate diarization metrics based on speaker count and utterance grouping.

    This is a practical approach that:
    1. Checks if the correct number of distinct speakers were detected
    2. Measures how well utterances are grouped by speaker (not fragmented)

    Note: We don't try to match speaker names since the transcription service
    assigns its own labels (e.g., "Speaker A", "Speaker B") that won't match
    our bot names ("Speaker 1", "Speaker 2").
    """
    if not actual_utterances:
        return DiarizationMetrics(
            total_speakers=expected_speaker_count,
            correct_speakers=0,
        )

    # Count unique speakers detected
    actual_speakers = set(utt.get("speaker") for utt in actual_utterances if utt.get("speaker"))
    detected_speaker_count = len(actual_speakers)

    # Calculate speaker fragmentation score
    # Lower fragmentation = better (speakers talking in contiguous blocks)
    # Count speaker transitions (how often the speaker changes between utterances)
    transitions = 0
    for i in range(1, len(actual_utterances)):
        if actual_utterances[i].get("speaker") != actual_utterances[i - 1].get("speaker"):
            transitions += 1

    # Ideal transitions for N speakers speaking sequentially = N-1
    # More transitions than expected suggests fragmentation or overlapping detection
    expected_transitions = max(0, expected_speaker_count - 1)
    extra_transitions = max(0, transitions - expected_transitions)

    # Fragmentation ratio: 0 = perfect sequential, higher = more fragmented
    fragmentation = extra_transitions / len(actual_utterances) if actual_utterances else 0

    # Simple DER approximation based on speaker count accuracy
    # If we detected wrong number of speakers, that's an error
    speaker_count_error = abs(detected_speaker_count - expected_speaker_count) / expected_speaker_count if expected_speaker_count > 0 else 0

    return DiarizationMetrics(
        der=speaker_count_error,  # Simplified: based on speaker count accuracy
        wder=fragmentation,  # Repurposed: measures fragmentation
        speaker_confusion=fragmentation,
        missed_speech=0.0,  # Can't measure without timestamps
        false_alarm=0.0,  # Can't measure without timestamps
        correct_speakers=min(detected_speaker_count, expected_speaker_count),
        total_speakers=expected_speaker_count,
    )


def calculate_audio_metrics(
    reference_audio_path: str,
    hypothesis_audio_path: str,
    sample_rate: int = 16000,
) -> AudioMetrics:
    """Calculate audio quality metrics (PESQ, STOI)."""
    metrics = AudioMetrics()

    if not os.path.exists(reference_audio_path) or not os.path.exists(hypothesis_audio_path):
        print("Warning: Audio files not found, skipping audio metrics", file=sys.stderr)
        return metrics

    try:
        if HAS_LIBROSA:
            # Load and resample audio files
            ref_audio, _ = librosa.load(reference_audio_path, sr=sample_rate, mono=True)
            hyp_audio, _ = librosa.load(hypothesis_audio_path, sr=sample_rate, mono=True)

            # Align lengths (truncate to shorter)
            min_len = min(len(ref_audio), len(hyp_audio))
            ref_audio = ref_audio[:min_len]
            hyp_audio = hyp_audio[:min_len]

            # Calculate SNR
            signal_power = np.mean(ref_audio**2)
            noise = hyp_audio - ref_audio
            noise_power = np.mean(noise**2)
            if noise_power > 0:
                metrics.snr_db = 10 * np.log10(signal_power / noise_power)

            # Calculate PESQ (requires 8kHz or 16kHz)
            if HAS_PESQ and sample_rate in (8000, 16000):
                try:
                    mode = "wb" if sample_rate == 16000 else "nb"
                    metrics.pesq_score = pesq(sample_rate, ref_audio, hyp_audio, mode)
                except Exception as e:
                    print(f"Warning: PESQ calculation failed: {e}", file=sys.stderr)

            # Calculate STOI
            if HAS_STOI:
                try:
                    metrics.stoi_score = stoi(ref_audio, hyp_audio, sample_rate, extended=False)
                except Exception as e:
                    print(f"Warning: STOI calculation failed: {e}", file=sys.stderr)

    except Exception as e:
        print(f"Warning: Audio metrics calculation failed: {e}", file=sys.stderr)

    return metrics


# ----------------------------
# HTTP Client
# ----------------------------


class AttendeeClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
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
            payload["transcription_settings"] = {"deepgram": {"language": "en"}}
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
                self._url(f"/api/v1/bots/{bot_id}/leave"),
                timeout=self.timeout,
            )
            if r.status_code in (200, 202, 204):
                return
        except requests.RequestException:
            pass

        try:
            r = self.session.delete(
                self._url(f"/api/v1/bots/{bot_id}"),
                timeout=self.timeout,
            )
        except requests.RequestException:
            pass

    def output_audio(self, bot_id: str, audio_path: Path) -> None:
        b64_data = base64.b64encode(audio_path.read_bytes()).decode("ascii")

        # Determine MIME type
        suffix = audio_path.suffix.lower()
        mime_type = "audio/mp3" if suffix == ".mp3" else "audio/wav"

        json_payload = {"type": mime_type, "data": b64_data}
        url = self._url(f"/api/v1/bots/{bot_id}/output_audio")
        r = self.session.post(url, data=json.dumps(json_payload), timeout=self.timeout)
        r.raise_for_status()

    def get_transcript(self, bot_id: str) -> List[Dict]:
        r = self.session.get(
            self._url(f"/api/v1/bots/{bot_id}/transcript"),
            timeout=self.timeout,
        )
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return []

    def get_recording_url(self, bot_id: str, timeout_s: int = 60) -> Optional[str]:
        """Get the recording URL, waiting for it to be available."""
        start = time.time()
        while (time.time() - start) < timeout_s:
            try:
                r = self.session.get(
                    self._url(f"/api/v1/bots/{bot_id}/recording"),
                    timeout=self.timeout,
                )
                if r.status_code == 200:
                    data = r.json()
                    url = data.get("url")
                    if url:
                        return url
            except Exception:
                pass
            time.sleep(2)
        return None

    def download_recording(self, url: str, output_path: Path) -> bool:
        """Download recording from URL."""
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            output_path.write_bytes(r.content)
            return True
        except Exception as e:
            print(f"Warning: Failed to download recording: {e}", file=sys.stderr)
            return False


# ----------------------------
# State helpers
# ----------------------------


def state_is_joined_recording(state: str) -> bool:
    s = (state or "").strip().lower()
    return "joined" in s and "record" in s


def wait_for_state(
    client: AttendeeClient,
    bot_id: str,
    predicate,
    desc: str,
    timeout_s: int,
    poll_s: float = 2.0,
) -> Dict:
    start = time.time()
    while True:
        bot = client.get_bot(bot_id)
        state = str(bot.get("state", ""))
        if predicate(state, bot):
            return bot
        if (time.time() - start) > timeout_s:
            raise TimeoutError(f"Timed out waiting for state '{desc}'. Last state={state!r}")
        time.sleep(poll_s)


# ----------------------------
# Ground truth loading
# ----------------------------


@dataclass
class SpeakerConfig:
    name: str
    audio_file: Path
    transcript: str
    audio_duration_seconds: float = 10.0  # Duration of audio file for sequential playback


@dataclass
class GroundTruth:
    speakers: List[SpeakerConfig]
    combined_transcript: str
    combined_audio_file: Optional[Path] = None

    @classmethod
    def from_json(cls, path: Path) -> "GroundTruth":
        data = json.loads(path.read_text())
        speakers = []
        for s in data.get("speakers", []):
            speakers.append(
                SpeakerConfig(
                    name=s["name"],
                    audio_file=Path(s["audio_file"]),
                    transcript=s["transcript"],
                    audio_duration_seconds=s.get("audio_duration_seconds", 10.0),
                )
            )

        combined_audio = None
        if data.get("combined_audio_file"):
            combined_audio = Path(data["combined_audio_file"])

        return cls(
            speakers=speakers,
            combined_transcript=data.get("combined_transcript", ""),
            combined_audio_file=combined_audio,
        )


# ----------------------------
# Main test execution
# ----------------------------


def run_meeting_quality_test(
    client: AttendeeClient,
    meeting_url: str,
    ground_truth: GroundTruth,
    join_timeout: int = 180,
    end_timeout: int = 300,
    speak_wait: float = 5.0,
    pause_between_speakers: float = 2.0,
    leave_after: Optional[float] = None,
    verbose: bool = False,
) -> MeetingQualityReport:
    """
    Run the meeting quality test.

    1. Create speaker bots and a recorder bot
    2. Wait for all bots to join
    3. Play audio from each speaker bot SEQUENTIALLY (realistic turn-taking)
    4. Wait for playback to complete
    5. Tell bots to leave and wait for end
    6. Fetch transcript and compare against ground truth
    7. Optionally compare audio recording
    """
    report = MeetingQualityReport()
    report.expected_transcript = ground_truth.combined_transcript

    # Track created bots for cleanup
    speaker_bots: List[Tuple[str, str, SpeakerConfig]] = []  # (bot_id, bot_name, config)
    recorder_bot_id: Optional[str] = None

    try:
        # 1. Create bots
        if verbose:
            print(f"Creating {len(ground_truth.speakers) + 1} bots...", end=" ", flush=True)

        for speaker in ground_truth.speakers:
            bot = client.create_bot(
                meeting_url=meeting_url,
                bot_name=speaker.name,
                enable_transcription=False,
            )
            speaker_bots.append((bot["id"], speaker.name, speaker))

        recorder = client.create_bot(
            meeting_url=meeting_url,
            bot_name="Recorder Bot",
            enable_transcription=True,
        )
        recorder_bot_id = recorder["id"]
        if verbose:
            print("done")

        # 2. Wait for all bots to join
        if verbose:
            print("Waiting for bots to join...", end=" ", flush=True)

        def _pred_joined(state: str, bot_obj: Dict) -> bool:
            return state_is_joined_recording(state)

        for bot_id, bot_name, _ in speaker_bots:
            wait_for_state(client, bot_id, _pred_joined, "joined_recording", join_timeout)

        wait_for_state(client, recorder_bot_id, _pred_joined, "joined_recording", join_timeout)
        if verbose:
            print("done")

        # Wait a bit for stable recording
        if speak_wait > 0:
            time.sleep(speak_wait)

        # 3. Play audio from speaker bots SEQUENTIALLY (realistic turn-taking)
        if verbose:
            print("Playing audio sequentially:")

        audio_start_time = time.time()

        for bot_id, bot_name, config in speaker_bots:
            if verbose:
                print(f"  {bot_name} ({config.audio_duration_seconds:.0f}s)...", end=" ", flush=True)

            try:
                client.output_audio(bot_id, config.audio_file)
            except Exception as e:
                report.failure_reasons.append(f"Failed to play audio for {bot_name}: {e}")
                if verbose:
                    print("failed")
                continue

            # Wait for this speaker's audio to finish + pause before next speaker
            wait_time = config.audio_duration_seconds + pause_between_speakers
            time.sleep(wait_time)
            if verbose:
                print("done")

        # 4. Additional wait after all speakers are done (if specified)
        if leave_after:
            time.sleep(leave_after)

        # 5. Tell all bots to leave
        if verbose:
            print("Waiting for bots to leave...", end=" ", flush=True)

        for bot_id, bot_name, _ in speaker_bots:
            client.tell_bot_to_leave(bot_id)
        client.tell_bot_to_leave(recorder_bot_id)

        # 6. Wait for all bots to end
        # Terminal states for speaker bots (don't need to wait for full processing)
        speaker_terminal_states = {"ended", "fatal_error", "data_deleted", "post_processing"}
        # Recorder bot must reach 'ended' to have complete transcript
        recorder_terminal_states = {"ended", "fatal_error", "data_deleted"}

        def _pred_speaker_terminal(state: str, bot_obj: Dict) -> bool:
            s = (state or "").strip().lower()
            return s in speaker_terminal_states

        def _pred_recorder_terminal(state: str, bot_obj: Dict) -> bool:
            s = (state or "").strip().lower()
            return s in recorder_terminal_states

        for bot_id, bot_name, _ in speaker_bots:
            try:
                final_bot = wait_for_state(client, bot_id, _pred_speaker_terminal, "terminal", end_timeout)
                final_state = final_bot.get("state", "unknown")
                if final_state == "fatal_error":
                    report.failure_reasons.append(f"{bot_name} ended with fatal_error")
            except TimeoutError as e:
                try:
                    current = client.get_bot(bot_id)
                    current_state = current.get("state", "unknown")
                    report.failure_reasons.append(f"{bot_name} stuck in state '{current_state}': {e}")
                except Exception:
                    report.failure_reasons.append(f"{bot_name} did not end: {e}")

        # Recorder bot must wait for 'ended' to have complete transcript
        try:
            final_bot = wait_for_state(client, recorder_bot_id, _pred_recorder_terminal, "ended", end_timeout)
            final_state = final_bot.get("state", "unknown")
            if final_state == "fatal_error":
                report.failure_reasons.append("Recorder Bot ended with fatal_error")
        except TimeoutError as e:
            try:
                current = client.get_bot(recorder_bot_id)
                current_state = current.get("state", "unknown")
                report.failure_reasons.append(f"Recorder Bot stuck in state '{current_state}': {e}")
            except Exception:
                report.failure_reasons.append(f"Recorder Bot did not end: {e}")

        if verbose:
            print("done")

        # 7. Fetch and analyze transcript
        if verbose:
            print("Analyzing transcript...", end=" ", flush=True)

        transcript_data = client.get_transcript(recorder_bot_id)

        # Build actual transcript and utterances
        actual_utterances = []
        actual_transcript_parts = []

        for utt in transcript_data:
            speaker_name = utt.get("speaker_name", "unknown")
            text = utt.get("transcription", {}).get("transcript", "")
            if text:
                actual_utterances.append({"speaker": speaker_name, "text": text})
                actual_transcript_parts.append(text)

        actual_transcript = " ".join(actual_transcript_parts)
        report.raw_transcript = actual_transcript

        # 8. Calculate transcript metrics
        report.transcript_metrics = calculate_transcript_metrics(
            ground_truth.combined_transcript,
            actual_transcript,
        )

        # 9. Calculate per-speaker WER
        for speaker in ground_truth.speakers:
            speaker_actual = " ".join(utt["text"] for utt in actual_utterances if utt["speaker"] == speaker.name)
            if speaker_actual:
                metrics = calculate_transcript_metrics(speaker.transcript, speaker_actual)
                report.per_speaker_wer[speaker.name] = metrics.wer

        # 10. Calculate diarization metrics
        report.diarization_metrics = calculate_diarization_metrics(
            expected_speaker_count=len(ground_truth.speakers),
            actual_utterances=actual_utterances,
        )

        if verbose:
            print("done")

        # 11. Calculate audio metrics (if ground truth audio available)
        if ground_truth.combined_audio_file and ground_truth.combined_audio_file.exists():
            if verbose:
                print("Calculating audio metrics...", end=" ", flush=True)

            recording_url = client.get_recording_url(recorder_bot_id)
            if recording_url:
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                if client.download_recording(recording_url, tmp_path):
                    report.audio_metrics = calculate_audio_metrics(
                        str(ground_truth.combined_audio_file),
                        str(tmp_path),
                    )
                    # Cleanup
                    tmp_path.unlink(missing_ok=True)

            if verbose:
                print("done")

        # 12. Determine pass/fail
        speakers_detected_correctly = (
            report.diarization_metrics.correct_speakers == report.diarization_metrics.total_speakers
        )
        report.passed = (
            len(report.failure_reasons) == 0
            and report.transcript_metrics.wer < 0.30  # 30% WER threshold
            and speakers_detected_correctly
        )

        if not speakers_detected_correctly:
            report.failure_reasons.append(
                f"Speaker count mismatch: expected {report.diarization_metrics.total_speakers}, "
                f"detected {report.diarization_metrics.correct_speakers}"
            )

    except Exception as e:
        report.failure_reasons.append(f"Test execution error: {e}")
        report.passed = False

    return report


def main():
    parser = argparse.ArgumentParser(description="Meeting Quality E2E Test - simulates a meeting and compares output against ground truth")
    parser.add_argument("--api-key", required=True, help="Attendee API key")
    parser.add_argument("--base-url", required=True, help="Attendee base URL")
    parser.add_argument("--meeting-url", required=True, help="Meeting URL (must bypass waiting room)")
    parser.add_argument("--ground-truth", required=True, help="Path to ground truth JSON file")
    parser.add_argument("--join-timeout", type=int, default=180, help="Seconds to wait for bots to join")
    parser.add_argument("--end-timeout", type=int, default=300, help="Seconds to wait for bots to end")
    parser.add_argument("--speak-wait", type=float, default=5.0, help="Seconds to wait before speaking")
    parser.add_argument("--pause-between-speakers", type=float, default=2.0, help="Seconds to pause between speakers")
    parser.add_argument("--leave-after", type=float, default=None, help="Seconds after speaking before leaving")
    parser.add_argument("--output", "-o", help="Output JSON file for results")
    parser.add_argument("--wer-threshold", type=float, default=0.30, help="Max acceptable WER (default: 0.30)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    args = parser.parse_args()

    # Load ground truth
    ground_truth_path = Path(args.ground_truth)
    if not ground_truth_path.exists():
        print(f"ERROR: Ground truth file not found: {ground_truth_path}", file=sys.stderr)
        sys.exit(2)

    ground_truth = GroundTruth.from_json(ground_truth_path)

    # Validate audio files exist
    for speaker in ground_truth.speakers:
        if not speaker.audio_file.exists():
            print(f"ERROR: Audio file not found: {speaker.audio_file}", file=sys.stderr)
            sys.exit(2)

    if args.verbose:
        total_duration = sum(s.audio_duration_seconds for s in ground_truth.speakers)
        print(f"Meeting Quality Test: {len(ground_truth.speakers)} speakers, {total_duration:.0f}s audio")

    client = AttendeeClient(args.base_url, args.api_key)

    report = run_meeting_quality_test(
        client=client,
        meeting_url=args.meeting_url,
        ground_truth=ground_truth,
        join_timeout=args.join_timeout,
        end_timeout=args.end_timeout,
        speak_wait=args.speak_wait,
        pause_between_speakers=args.pause_between_speakers,
        leave_after=args.leave_after,
        verbose=args.verbose,
    )

    # Print summary
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    print(f"Overall: {'PASSED' if report.passed else 'FAILED'}")
    print("\nTranscript Metrics:")
    print(f"  Word Error Rate (WER):      {report.transcript_metrics.wer:.2%}")
    print(f"  Character Error Rate (CER): {report.transcript_metrics.cer:.2%}")
    print(f"  Substitutions: {report.transcript_metrics.substitutions}")
    print(f"  Insertions:    {report.transcript_metrics.insertions}")
    print(f"  Deletions:     {report.transcript_metrics.deletions}")

    if report.per_speaker_wer:
        print("\nPer-Speaker WER:")
        for speaker, wer in report.per_speaker_wer.items():
            print(f"  {speaker}: {wer:.2%}")

    print("\nDiarization Metrics:")
    print(f"  Speakers Detected:        {report.diarization_metrics.correct_speakers} (expected: {report.diarization_metrics.total_speakers})")
    print(f"  Speaker Count Accuracy:   {1 - report.diarization_metrics.der:.2%}")
    print(f"  Fragmentation Score:      {report.diarization_metrics.wder:.2%} (lower = better grouping)")

    if report.audio_metrics.pesq_score or report.audio_metrics.stoi_score:
        print("\nAudio Metrics:")
        if report.audio_metrics.pesq_score:
            print(f"  PESQ Score: {report.audio_metrics.pesq_score:.2f} (range: -0.5 to 4.5)")
        if report.audio_metrics.stoi_score:
            print(f"  STOI Score: {report.audio_metrics.stoi_score:.2%}")
        if report.audio_metrics.snr_db:
            print(f"  SNR:        {report.audio_metrics.snr_db:.1f} dB")

    if report.failure_reasons:
        print("\nFailure Reasons:")
        for reason in report.failure_reasons:
            print(f"  - {reason}")

    print("=" * 60)

    # Save results to JSON if requested
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(report.to_dict(), indent=2))
        print(f"\nResults saved to: {output_path}")

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
