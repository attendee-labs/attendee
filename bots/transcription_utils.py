import copy
import io
import logging
import os
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Sequence

import requests

from bots.models import Credentials, ParticipantEvent, ParticipantEventTypes, Recording, TranscriptionFailureReasons, TranscriptionSettings, Utterance

logger = logging.getLogger(__name__)


def is_retryable_failure(failure_data):
    return failure_data.get("reason") in [
        TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED,
        TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED,
        TranscriptionFailureReasons.TIMED_OUT,
        TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED,
        TranscriptionFailureReasons.INTERNAL_ERROR,
    ]


def get_empty_transcript_for_utterance_group(utterances):
    # Forms a dict that maps utterance id to empty transcript
    return {utterance.id: {"transcript": "", "words": []} for utterance in utterances}


def get_mp3_for_utterance_group(
    utterances: Sequence[Utterance],
    *,
    silence_seconds: float = 3.0,
    channels: int = 1,
    sample_rate: int,
    sample_width_bytes: int = 2,  # 2 => 16-bit PCM (s16le)
    bitrate_kbps: int = 128,
    io_chunk_bytes: int = 256 * 1024,
) -> bytes:
    """
    Given an array of Utterance instances whose audio blobs are ALWAYS RAW PCM,
    returns an MP3 (as bytes) containing each utterance concatenated with `silence_seconds`
    of silence between them.

    Streaming properties:
      - PCM is streamed into ffmpeg stdin in chunks (no concatenation of PCM in memory).
      - Silence is streamed as zero-bytes in chunks.
      - MP3 is read from ffmpeg stdout in chunks.

    Important note:
      - Returning `bytes` inherently means the final MP3 is fully held in memory at the end.
        Its size is roughly (bitrate_kbps / 8) * duration_seconds (plus small overhead).

    Assumptions:
      - PCM is signed 16-bit little-endian (s16le). If yours differs (e.g. float32), change -f/-sample_width_bytes.
      - All utterances share the same sample rate (enforced), unless `sample_rate` is provided.

    Raises:
      - ValueError / RuntimeError on invalid inputs or ffmpeg failure.
    """
    if not utterances:
        raise ValueError("No utterances provided.")

    target_sr = sample_rate

    bytes_per_second = target_sr * int(channels) * int(sample_width_bytes)
    total_silence_bytes = int(round(float(silence_seconds) * bytes_per_second))

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        # input: raw pcm from stdin
        "-f",
        "s16le",
        "-ar",
        str(target_sr),
        "-ac",
        str(int(channels)),
        "-i",
        "pipe:0",
        # output: mp3 to stdout
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{int(bitrate_kbps)}k",
        "-f",
        "mp3",
        "pipe:1",
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,  # unbuffered pipes
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    writer_exc: list[BaseException] = []

    def _write_pcm_and_silence() -> None:
        try:
            zero_chunk = b"\x00" * min(io_chunk_bytes, 256 * 1024)

            def write_buf(buf: memoryview) -> None:
                for off in range(0, len(buf), io_chunk_bytes):
                    proc.stdin.write(buf[off : off + io_chunk_bytes])

            def write_silence(nbytes: int) -> None:
                remaining = nbytes
                while remaining > 0:
                    take = min(len(zero_chunk), remaining)
                    proc.stdin.write(zero_chunk[:take])
                    remaining -= take

            for utterance_index, utterance in enumerate(utterances):
                if utterance.get_sample_rate() != target_sr:
                    raise ValueError(f"Sample rate mismatch: utterance {utterance.id} has {utterance.get_sample_rate()}, expected {target_sr}.")

                if utterance_index > 0:
                    write_silence(total_silence_bytes)

                blob = utterance.get_audio_blob()
                if blob is None:
                    raise ValueError(f"Utterance {utterance.id} has no audio_blob.")
                write_buf(memoryview(blob))

        except BaseException as e:
            writer_exc.append(e)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    t = threading.Thread(target=_write_pcm_and_silence, name="ffmpeg_pcm_writer", daemon=True)
    t.start()

    # Read MP3 output while writer thread feeds stdin (prevents deadlocks on full stdout buffers).
    out = io.BytesIO()
    try:
        while True:
            chunk = proc.stdout.read(io_chunk_bytes)
            if not chunk:
                break
            out.write(chunk)

        rc = proc.wait()
        t.join()

        if writer_exc:
            # Prefer writer error (sample-rate mismatch, missing blob, etc.)
            raise writer_exc[0]

        if rc != 0:
            err = (proc.stderr.read() or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg failed (exit {rc}). stderr:\n{err}")

        return out.getvalue()

    finally:
        try:
            proc.kill()
        except Exception:
            pass


from dataclasses import dataclass


@dataclass(frozen=True)
class _Interval:
    participant: Any
    start: float  # seconds
    end: float  # seconds
    index: int  # stable tie-breaker


def split_transcription_by_speaker_events(
    transcription_result: dict,
    speaker_events: Sequence[ParticipantEvent],
    first_buffer_timestamp_ms: int,
) -> list[dict]:
    """
    Split a full-recording transcription into per-speaker utterance chunks.

    Each word is assigned to the nearest speech interval (by midpoint).
    Consecutive words in the same interval are grouped into one utterance.
    """
    language = transcription_result.get("language")
    words = list(transcription_result.get("words") or [])
    if not words:
        return []

    words.sort(key=lambda w: (w["start"], w["end"]))
    recording_end = max(w["end"] for w in words)

    intervals = _split_transcription_by_speaker_events_build_intervals(speaker_events, recording_end, first_buffer_timestamp_ms)
    if not intervals:
        return []

    intervals.sort(key=lambda it: (it.start, it.index))

    # Assign words → intervals → group consecutive runs into utterances
    utterances = []
    prev_idx = None
    current_words = []

    for word in words:
        midpoint = (word["start"] + word["end"]) / 2.0
        logger.info("Word: %s", word)
        logger.info("Midpoint: %s", midpoint)

        ranked = _split_transcription_by_speaker_events_rank_intervals(midpoint, intervals, top_n=5)
        for rank, (ri, iv, dist_key) in enumerate(ranked):
            logger.info(
                "  Nearest #%d: idx=%d participant=%s [%.3f–%.3f] distance_key=%s",
                rank + 1,
                ri,
                iv.participant,
                iv.start,
                iv.end,
                dist_key,
            )

        idx = _split_transcription_by_speaker_events_nearest_interval(midpoint, intervals)
        chosen = intervals[idx]
        logger.info(
            "  Chosen interval: idx=%d participant=%s [%.3f–%.3f]",
            idx,
            chosen.participant,
            chosen.start,
            chosen.end,
        )

        if idx == prev_idx:
            current_words.append(word)
        else:
            if current_words:
                utterances.append(_split_transcription_by_speaker_events_make_utterance(intervals[prev_idx], current_words, language))
            prev_idx = idx
            current_words = [word]

    if current_words:
        utterances.append(_split_transcription_by_speaker_events_make_utterance(intervals[prev_idx], current_words, language))

    return utterances


def _split_transcription_by_speaker_events_build_intervals(events: list, recording_end: float, first_buffer_timestamp_ms: int) -> list[_Interval]:
    """Convert SPEECH_START/STOP events into closed intervals in audio-relative time."""
    events = sorted(
        events,
        key=lambda ev: (
            ev.timestamp_ms,
            0 if ev.event_type == ParticipantEventTypes.SPEECH_START else 1,
            ev.participant_id,
        ),
    )

    active: dict[int, tuple[float, Any]] = {}  # pid → (start_sec, participant)
    intervals: list[_Interval] = []

    for ev in events:
        if ev.event_type not in (ParticipantEventTypes.SPEECH_START, ParticipantEventTypes.SPEECH_STOP):
            continue

        pid = ev.participant_id
        t = (ev.timestamp_ms - first_buffer_timestamp_ms) / 1000.0

        if ev.event_type == ParticipantEventTypes.SPEECH_START:
            # Close any already-active interval for this participant
            if pid in active:
                start, participant = active[pid]
                start_adjusted = start
                end_adjusted = t
                if end_adjusted > start_adjusted:
                    intervals.append(_Interval(participant, start_adjusted, end_adjusted, len(intervals)))
            active[pid] = (t, ev.participant)

        else:  # SPEECH_STOP
            if pid not in active:
                continue
            start, participant = active.pop(pid)
            start_adjusted = start
            end_adjusted = t - 0.35
            if end_adjusted > start_adjusted:
                intervals.append(_Interval(participant, start_adjusted, end_adjusted, len(intervals)))

    # Close any still-open intervals at end of recording
    for pid, (start, participant) in active.items():
        intervals.append(_Interval(participant, start, recording_end, len(intervals)))

    return intervals


def _split_transcription_by_speaker_events_nearest_interval(t: float, intervals: list[_Interval]) -> int:
    """Return index of the interval nearest to time t. Ties go to earlier start."""
    best = None
    best_key = None

    for i, iv in enumerate(intervals):
        if iv.start <= t <= iv.end:
            key = (0, 0.0, iv.start, iv.index)
        elif t < iv.start:
            key = (1, iv.start - t, iv.start, iv.index)
        else:
            key = (1, t - iv.end, iv.start, iv.index)

        if best_key is None or key < best_key:
            best = i
            best_key = key

    return best


def _split_transcription_by_speaker_events_rank_intervals(t: float, intervals: list[_Interval], top_n: int = 5) -> list[tuple[int, _Interval, tuple]]:
    """Return the top_n intervals nearest to time t, sorted by distance key (closest first)."""
    scored = []
    for i, iv in enumerate(intervals):
        if iv.start <= t <= iv.end:
            key = (0, 0.0, iv.start, iv.index)
        elif t < iv.start:
            key = (1, iv.start - t, iv.start, iv.index)
        else:
            key = (1, t - iv.end, iv.start, iv.index)
        scored.append((key, i, iv))

    scored.sort(key=lambda x: x[0])
    return [(i, iv, key) for key, i, iv in scored[:top_n]]


def _split_transcription_by_speaker_events_make_utterance(interval: _Interval, words: list[dict], language: str | None) -> dict:
    utterance_start = words[0]["start"]
    adjusted_words = [{**w, "start": w["start"] - utterance_start, "end": w["end"] - utterance_start} for w in words]
    return {
        "participant": interval.participant,
        "transcription": {
            "transcript": " ".join(w.get("word", "") for w in adjusted_words).strip(),
            "words": adjusted_words,
            "language": language,
        },
        "start_time": utterance_start,
        "duration": words[-1]["end"] - utterance_start,
    }


def split_transcription_by_ml_diarization(
    transcription_result: dict,
    speaker_events: Sequence[ParticipantEvent],
) -> list[dict]:
    """
    Split a full-recording transcription into per-speaker utterance chunks
    using AssemblyAI's speaker_labels (the 'speaker' attribute on each word).

    Speaker label 'A' maps to the first participant with any speaking events,
    'B' maps to the second, and so on (ordered by first SPEECH_START timestamp).

    Output format matches split_transcription_by_speaker_events.
    """
    language = transcription_result.get("language")
    words = list(transcription_result.get("words") or [])
    if not words:
        return []

    words.sort(key=lambda w: (w["start"], w["end"]))

    # Build speaker label → participant mapping based on the order participants
    # first appear in SPEECH_START events.
    seen = set()
    ordered_participants = []
    for ev in sorted(speaker_events, key=lambda e: e.timestamp_ms):
        if ev.event_type != ParticipantEventTypes.SPEECH_START:
            continue
        if ev.participant_id not in seen:
            seen.add(ev.participant_id)
            ordered_participants.append(ev.participant)

    label_to_participant = {}
    for i, participant in enumerate(ordered_participants):
        label = chr(ord("A") + i)
        label_to_participant[label] = participant

    utterances = []
    prev_speaker = None
    current_words = []

    for word in words:
        speaker = word.get("speaker")
        if speaker == prev_speaker:
            current_words.append(word)
        else:
            if current_words and prev_speaker is not None:
                participant = label_to_participant.get(prev_speaker)
                if participant is not None:
                    utterances.append(_make_diarized_utterance(participant, current_words, language))
            prev_speaker = speaker
            current_words = [word]

    if current_words and prev_speaker is not None:
        participant = label_to_participant.get(prev_speaker)
        if participant is not None:
            utterances.append(_make_diarized_utterance(participant, current_words, language))

    return utterances


def _make_diarized_utterance(participant, words: list[dict], language: str | None) -> dict:
    utterance_start = words[0]["start"]
    adjusted_words = [{**w, "start": w["start"] - utterance_start, "end": w["end"] - utterance_start} for w in words]
    return {
        "participant": participant,
        "transcription": {
            "transcript": " ".join(w.get("word", "") for w in adjusted_words).strip(),
            "words": adjusted_words,
            "language": language,
        },
        "start_time": utterance_start,
        "duration": words[-1]["end"] - utterance_start,
    }


def split_transcription_by_utterance(
    transcription_result: Dict[str, Any],
    utterances: Sequence[Utterance],
    *,
    silence_seconds: float = 3.0,
) -> Dict[int, Dict[str, Any]]:
    """
    Split transcription result from a combined MP3 back into per-utterance results.

    Assumes:
      - utterances were concatenated in THIS order
      - each utterance contributes duration_ms / 1000.0 seconds of audio
      - exactly `silence_seconds` of silence was inserted between utterances

    Returns:
      { utterance_id: {"transcript": str, "words": [...], "language": str|None} }
    """
    if not utterances:
        return {}

    language = transcription_result.get("language")
    words = transcription_result.get("words") or []

    # Build utterance time windows in the combined audio.
    windows: List[tuple[int, float, float]] = []
    t = 0.0
    for u in utterances:
        dur_s = u.duration_ms / 1000.0
        start = t
        end = start + dur_s
        windows.append((u.id, start, end))
        t = end + silence_seconds

    output = {utterance.id: {"transcript": "", "words": [], "language": language} for utterance in utterances}

    # Assign each word to the first window it overlaps with.
    word_index = 0
    for window_index, (utterance_id, start, end) in enumerate(windows):
        utterance_words = []
        next_start = windows[window_index + 1][1] if window_index + 1 < len(windows) else None

        while word_index < len(words):
            w = words[word_index]
            # If word starts at or after window end, stop (no overlap with this window)
            if w["start"] >= end:
                break
            # If word ends after window start, it overlaps
            if w["end"] > start:
                # Check that word doesn't also overlap with next window (unexpected)
                if next_start is not None and w["end"] > next_start:
                    logger.warning(f"Word overlaps with subsequent window, skipping: {w}")
                else:
                    # Create a new word object with the start and end times adjusted to the current window
                    word_adjusted = dict(w)
                    word_adjusted["start"] = word_adjusted["start"] - start
                    word_adjusted["end"] = word_adjusted["end"] - start
                    utterance_words.append(word_adjusted)
            word_index += 1

        output[utterance_id]["words"] = utterance_words
        output[utterance_id]["transcript"] = " ".join(w["word"] for w in utterance_words)

    return output


def get_transcription_via_assemblyai_for_utterance_group(utterances):
    first_utterance = utterances[0]
    total_duration_ms = sum(utterance.duration_ms for utterance in utterances)

    transcription, error = get_transcription_via_assemblyai_from_mp3(
        retrieve_mp3_data_callback=lambda: get_mp3_for_utterance_group(utterances, sample_rate=first_utterance.get_sample_rate()),
        duration_ms=total_duration_ms,
        identifier=f"utterances {[utterance.id for utterance in utterances]}",
        transcription_settings=first_utterance.transcription_settings,
        recording=first_utterance.recording,
    )

    if error:
        return None, error

    return split_transcription_by_utterance(transcription, utterances), None


def get_transcription_via_assemblyai_using_speaker_events(speaker_events, recording, transcription_settings):
    transcription, error = get_transcription_via_assemblyai_from_mp3(
        retrieve_mp3_data_url_callback=lambda: recording.url,
        duration_ms=(recording.completed_at - recording.started_at).total_seconds() * 1000,
        identifier=f"recording {recording.id}",
        transcription_settings=transcription_settings,
        recording=recording,
    )
    if error:
        return None, error

    return split_transcription_by_speaker_events(transcription, speaker_events, recording.first_buffer_timestamp_ms), None


def get_transcription_via_assemblyai_using_speaker_events_and_ml_diarization(speaker_events, recording, transcription_settings):
    # First get transcription using speaker events
    # then get transcription using ml diarization plus speaker events

    # If the results agree then use the ml-diarization result

    speaker_events_results, error = get_transcription_via_assemblyai_using_speaker_events(
        speaker_events=speaker_events,
        recording=recording,
        transcription_settings=transcription_settings,
    )

    if error:
        return None, error

    # Count the number of participants who had any speech according to the speaker events results
    num_participants_with_speech = len({utterance["participant"].id for utterance in speaker_events_results})

    # Make a new transcription settings object with the speaker labels and speakers expected set to the number of participants with speech
    transcription_settings_with_speaker_labels = TranscriptionSettings(copy.deepcopy(transcription_settings._settings))
    transcription_settings_with_speaker_labels._settings["assembly_ai"]["speaker_labels"] = True
    transcription_settings_with_speaker_labels._settings["assembly_ai"]["speakers_expected"] = num_participants_with_speech

    # Diarize the transcription using the speaker labels and speakers expected
    transcription_using_speaker_labels, error = get_transcription_via_assemblyai_from_mp3(
        retrieve_mp3_data_url_callback=lambda: recording.url,
        duration_ms=(recording.completed_at - recording.started_at).total_seconds() * 1000,
        identifier=f"recording {recording.id}",
        transcription_settings=transcription_settings_with_speaker_labels,
        recording=recording,
    )
    if error:
        logger.error(f"Error diarizing transcription using speaker labels and speakers expected: {error}. Using speaker events results instead.")
        return speaker_events_results, None

    ml_diarization_results = split_transcription_by_ml_diarization(transcription_using_speaker_labels, speaker_events)
    speaker_events_results_two = split_transcription_by_speaker_events(transcription_using_speaker_labels, speaker_events, recording.first_buffer_timestamp_ms)

    # If the ml diarization results agree with the speaker events results, then use the ml diarization results
    agreement = diarization_agreement(ml_diarization_results, speaker_events_results_two)
    logger.info(
        "Diarization agreement for recording %s: word_accuracy=%.3f duration_weighted=%.3f (%d words)",
        recording.id,
        agreement["word_accuracy"],
        agreement["duration_weighted_accuracy"],
        agreement["num_words"],
    )

    if agreement["duration_weighted_accuracy"] >= 0.85:
        logger.info(f"Diarization agreement for recording {recording.id} is {agreement['duration_weighted_accuracy']}. Using ML diarization results.")
        return ml_diarization_results, None

    logger.info(f"Diarization agreement for recording {recording.id} is {agreement['duration_weighted_accuracy']}. Using speaker events results.")
    return speaker_events_results, None


def diarization_agreement(results_a: list[dict], results_b: list[dict]) -> dict:
    """
    Compare two per-word speaker-classification results on the same set of words.

    Both inputs are the list-of-utterance-dicts format produced by
    split_transcription_by_speaker_events / split_transcription_by_ml_diarization.
    Each word in the underlying transcription is assigned to exactly one
    participant per result set. Because both functions sort words by
    (start, end) and then group contiguous runs by speaker, flattening the
    utterances back out yields the words in the same order in both inputs.

    Returns a dict with:
        num_words: total words compared
        num_agreements: words where both assigned the same participant
        word_accuracy: num_agreements / num_words (simple per-word agreement)
        duration_weighted_accuracy: agreement weighted by word duration
            (this is the complement of word-level Diarization Error Rate
            when the speaker identities already match, which they do here
            since both results use the same Participant objects)
        total_duration: sum of word durations considered
        agreement_duration: sum of durations where speakers agreed

    Raises ValueError if the two results do not cover the same set of words.
    """

    def flatten(results):
        out = []
        for utt in results:
            participant = utt["participant"]
            for w in utt["transcription"]["words"]:
                out.append((w, participant))
        return out

    flat_a = flatten(results_a)
    flat_b = flatten(results_b)

    if len(flat_a) != len(flat_b):
        raise ValueError(f"Word count mismatch: {len(flat_a)} vs {len(flat_b)}")

    num_words = len(flat_a)
    if num_words == 0:
        return {
            "num_words": 0,
            "num_agreements": 0,
            "word_accuracy": 1.0,
            "duration_weighted_accuracy": 1.0,
            "total_duration": 0.0,
            "agreement_duration": 0.0,
        }

    num_agreements = 0
    total_duration = 0.0
    agreement_duration = 0.0

    for (word_a, participant_a), (word_b, participant_b) in zip(flat_a, flat_b):
        # Sanity-check that flattened orderings line up. Word start/end in
        # each utterance are relative to that utterance, so compare by text.
        if word_a.get("word") != word_b.get("word"):
            raise ValueError(f"Word alignment mismatch: {word_a.get('word')!r} vs {word_b.get('word')!r}")

        duration = max(0.0, float(word_a["end"]) - float(word_a["start"]))
        total_duration += duration

        a_id = getattr(participant_a, "id", participant_a)
        b_id = getattr(participant_b, "id", participant_b)
        if a_id == b_id:
            num_agreements += 1
            agreement_duration += duration

    word_accuracy = num_agreements / num_words
    duration_weighted_accuracy = (agreement_duration / total_duration) if total_duration > 0 else word_accuracy

    return {
        "num_words": num_words,
        "num_agreements": num_agreements,
        "word_accuracy": word_accuracy,
        "duration_weighted_accuracy": duration_weighted_accuracy,
        "total_duration": total_duration,
        "agreement_duration": agreement_duration,
    }


def get_transcription_via_assemblyai_from_mp3(
    duration_ms: int,
    identifier: str,
    transcription_settings: TranscriptionSettings,
    recording: Recording,
    retrieve_mp3_data_callback: Callable[[], bytes] = None,
    retrieve_mp3_data_url_callback: Callable[[], str] = None,
):
    assemblyai_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.ASSEMBLY_AI).first()
    if not assemblyai_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    assemblyai_credentials = assemblyai_credentials_record.get_credentials()
    if not assemblyai_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    api_key = assemblyai_credentials.get("api_key")
    if not api_key:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": "api_key not in credentials"}

    # If the audio blob is less than 175ms in duration, just return an empty transcription
    # Audio clips this short are almost never generated, it almost certainly didn't have any speech
    # and if we send it to the assemblyai api, the upload will fail
    if duration_ms < 175:
        logger.info(f"AssemblyAI transcription skipped for {identifier} because it's less than 175ms in duration")
        return {"transcript": "", "words": []}, None

    headers = {"authorization": api_key}
    base_url = transcription_settings.assemblyai_base_url()

    upload_url = None
    if retrieve_mp3_data_url_callback:
        upload_url = retrieve_mp3_data_url_callback()

        if not upload_url:
            return None, {"reason": TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED, "error": "upload_url not found"}
    else:
        mp3_data = retrieve_mp3_data_callback()
        upload_response = requests.post(f"{base_url}/upload", headers=headers, data=mp3_data)

        if upload_response.status_code == 401:
            return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

        if upload_response.status_code != 200:
            return None, {"reason": TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED, "status_code": upload_response.status_code, "text": upload_response.text}

        upload_url = upload_response.json()["upload_url"]

    data = {
        "audio_url": upload_url,
        "speech_models": ["universal-3-pro", "universal-2"],
    }

    if transcription_settings.assembly_ai_language_detection():
        data["language_detection"] = True
    elif transcription_settings.assembly_ai_language_code():
        data["language_code"] = transcription_settings.assembly_ai_language_code()

    # Add keyterms_prompt and speech_model if set
    keyterms_prompt = transcription_settings.assemblyai_keyterms_prompt()
    if keyterms_prompt:
        data["keyterms_prompt"] = keyterms_prompt
    speech_model = transcription_settings.assemblyai_speech_model()
    if speech_model:
        if "speech_models" in data:
            del data["speech_models"]
        data["speech_model"] = speech_model
    speech_models = transcription_settings.assemblyai_speech_models()
    if speech_models:
        if "speech_model" in data:
            del data["speech_model"]
        data["speech_models"] = speech_models

    if transcription_settings.assemblyai_speaker_labels():
        data["speaker_labels"] = True

    speakers_expected = transcription_settings.assemblyai_speakers_expected()
    if speakers_expected:
        data["speakers_expected"] = speakers_expected

    language_detection_options = transcription_settings.assemblyai_language_detection_options()
    if language_detection_options:
        data["language_detection_options"] = language_detection_options

    url = f"{base_url}/transcript"
    response = requests.post(url, json=data, headers=headers)

    if response.status_code != 200:
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "text": response.text}

    transcript_id = response.json()["id"]
    polling_endpoint = f"{base_url}/transcript/{transcript_id}"

    # Poll the result_url until we get a completed transcription
    max_retries = int(os.getenv("TRANSCRIPTION_POLLING_TIMEOUT_SECONDS", 120))  # Maximum number of retries (2 minutes with 1s sleep)
    retry_count = 0

    while retry_count < max_retries:
        polling_response = requests.get(polling_endpoint, headers=headers)

        if polling_response.status_code != 200:
            logger.error(f"AssemblyAI result fetch failed with status code {polling_response.status_code}")
            time.sleep(10)
            retry_count += 10
            continue

        transcription_result = polling_response.json()

        if transcription_result["status"] == "completed":
            logger.info("AssemblyAI transcription completed successfully, now deleting from AssemblyAI.")

            # Delete the transcript from AssemblyAI
            delete_response = requests.delete(polling_endpoint, headers=headers)
            if delete_response.status_code != 200:
                logger.error(f"AssemblyAI delete failed with status code {delete_response.status_code}: {delete_response.text}")
            else:
                logger.info("AssemblyAI delete successful")

            transcript_text = transcription_result.get("text", "")
            words = transcription_result.get("words", [])

            formatted_words = []
            if words:
                for word in words:
                    formatted_word = {
                        "word": word["text"],
                        "start": word["start"] / 1000.0,
                        "end": word["end"] / 1000.0,
                        "confidence": word["confidence"],
                    }
                    if "speaker" in word:
                        formatted_word["speaker"] = word["speaker"]

                    formatted_words.append(formatted_word)

            transcription = {"transcript": transcript_text, "words": formatted_words, "language": transcription_result.get("language_code", None)}
            return transcription, None

        elif transcription_result["status"] == "error":
            error = transcription_result.get("error")

            if error and "language_detection cannot be performed on files with no spoken audio" in error:
                logger.info(f"AssemblyAI transcription skipped for {identifier} because it did not have any spoken audio and we tried to detect language")
                return {"transcript": "", "words": []}, None

            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "error": error}

        else:  # queued, processing
            logger.info(f"AssemblyAI transcription status: {transcription_result['status']}, waiting...")
            time.sleep(1)
            retry_count += 1

    # If we've reached here, we've timed out
    return None, {"reason": TranscriptionFailureReasons.TIMED_OUT, "step": "transcribe_result_poll"}
