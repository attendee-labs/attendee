import io
import logging
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Sequence

import requests

from bots.models import Credentials, Recording, TranscriptionFailureReasons, TranscriptionSettings, Utterance

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

    out = {utterance.id: {"transcript": "", "words": [], "language": language} for utterance in utterances}

    # Assign each word to the first window it overlaps with.
    w_idx = 0
    for utt_id, start, end in windows:
        utt_words = []

        while w_idx < len(words):
            w = words[w_idx]
            # If word starts at or after window end, stop (no overlap with this window)
            if w["start"] >= end:
                break
            # If word ends after window start, it overlaps
            if w["end"] > start:
                w2 = dict(w)
                w2["start"] = w2["start"] - start
                w2["end"] = w2["end"] - start
                utt_words.append(w2)
            w_idx += 1

        out[utt_id]["words"] = utt_words
        out[utt_id]["transcript"] = " ".join(w["word"] for w in utt_words)

    return out


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


def get_transcription_via_assemblyai_from_mp3(
    retrieve_mp3_data_callback: Callable[[], bytes],
    duration_ms: int,
    identifier: str,
    transcription_settings: TranscriptionSettings,
    recording: Recording,
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

    mp3_data = retrieve_mp3_data_callback()
    upload_response = requests.post(f"{base_url}/upload", headers=headers, data=mp3_data)

    if upload_response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

    if upload_response.status_code != 200:
        return None, {"reason": TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED, "status_code": upload_response.status_code, "text": upload_response.text}

    upload_url = upload_response.json()["upload_url"]

    data = {
        "audio_url": upload_url,
        "speech_model": "universal",
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
        data["speech_model"] = speech_model

    if transcription_settings.assemblyai_speaker_labels():
        data["speaker_labels"] = True

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
    max_retries = 120  # Maximum number of retries (2 minutes with 1s sleep)
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
