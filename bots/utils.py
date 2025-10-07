import io

import cv2
import numpy as np
from pydub import AudioSegment

from .meeting_url_utils import meeting_type_from_url
from .models import (
    MeetingTypes,
    TranscriptionProviders,
)


def pcm_to_mp3(
    pcm_data: bytes,
    sample_rate: int = 32000,
    channels: int = 1,
    sample_width: int = 2,
    bitrate: str = "128k",
    output_sample_rate: int = None,
) -> bytes:
    """
    Convert PCM audio data to MP3 format.

    Args:
        pcm_data (bytes): Raw PCM audio data
        sample_rate (int): Input sample rate in Hz (default: 32000)
        channels (int): Number of audio channels (default: 1)
        sample_width (int): Sample width in bytes (default: 2)
        bitrate (str): MP3 encoding bitrate (default: "128k")
        output_sample_rate (int): Output sample rate in Hz (default: None, uses input sample_rate)

    Returns:
        bytes: MP3 encoded audio data
    """
    # Create AudioSegment from raw PCM data
    audio_segment = AudioSegment(
        data=pcm_data,
        sample_width=sample_width,
        frame_rate=sample_rate,
        channels=channels,
    )

    # Resample to different sample rate if specified
    if output_sample_rate is not None and output_sample_rate != sample_rate:
        audio_segment = audio_segment.set_frame_rate(output_sample_rate)

    # Create a bytes buffer to store the MP3 data
    buffer = io.BytesIO()

    # Export the audio segment as MP3 to the buffer with specified bitrate
    audio_segment.export(buffer, format="mp3", parameters=["-b:a", bitrate])

    # Get the MP3 data as bytes
    mp3_data = buffer.getvalue()
    buffer.close()

    return mp3_data


def mp3_to_pcm(mp3_data: bytes, sample_rate: int = 32000, channels: int = 1, sample_width: int = 2) -> bytes:
    """
    Convert MP3 audio data to PCM format.

    Args:
        mp3_data (bytes): MP3 audio data
        sample_rate (int): Desired sample rate in Hz (default: 32000)
        channels (int): Desired number of audio channels (default: 1)
        sample_width (int): Desired sample width in bytes (default: 2)

    Returns:
        bytes: Raw PCM audio data
    """
    # Create a bytes buffer from the MP3 data
    buffer = io.BytesIO(mp3_data)

    # Load the MP3 data into an AudioSegment
    audio_segment = AudioSegment.from_mp3(buffer)

    # Convert to the desired format
    audio_segment = audio_segment.set_frame_rate(sample_rate)
    audio_segment = audio_segment.set_channels(channels)
    audio_segment = audio_segment.set_sample_width(sample_width)

    # Get the raw PCM data
    pcm_data = audio_segment.raw_data
    buffer.close()

    return pcm_data


def calculate_audio_duration_ms(audio_data: bytes, content_type: str) -> int:
    """
    Calculate the duration of audio data in milliseconds.

    Args:
        audio_data (bytes): Audio data in either PCM or MP3 format
        content_type (str): Content type of the audio data (e.g., 'audio/mp3')

    Returns:
        int: Duration in milliseconds
    """
    buffer = io.BytesIO(audio_data)

    if content_type == "audio/mp3":
        audio = AudioSegment.from_mp3(buffer)
    else:
        raise ValueError(f"Unsupported content type for duration calculation: {content_type}")

    buffer.close()
    # len(audio) returns duration in milliseconds for pydub AudioSegment objects
    duration_ms = len(audio)
    return duration_ms


def create_zero_pcm_audio(audio_format, duration_ms=250):
    """Create zero'd PCM audio for the given format and duration"""
    # Parse the audio format to get sample rate and format
    if "rate=32000" in audio_format:
        sample_rate = 32000
    elif "rate=48000" in audio_format:
        sample_rate = 48000
    else:
        # Default to 32000 if not specified
        sample_rate = 32000

    # Calculate number of samples for the duration
    samples_count = int((duration_ms / 1000.0) * sample_rate)

    if "format=S16LE" in audio_format:
        # 16-bit signed little endian
        zero_audio = np.zeros(samples_count, dtype=np.int16)
    elif "format=F32LE" in audio_format:
        # 32-bit float little endian
        zero_audio = np.zeros(samples_count, dtype=np.float32)
    else:
        # Default to S16LE
        zero_audio = np.zeros(samples_count, dtype=np.int16)

    return zero_audio.tobytes()


def create_black_i420_frame(video_frame_size):
    """Create a black I420 frame for the given dimensions"""
    width, height = video_frame_size
    # Ensure dimensions are even for proper chroma subsampling
    if width % 2 != 0 or height % 2 != 0:
        raise ValueError("Width and height must be even numbers for I420 format")

    # Y plane (black = 0 in Y plane)
    y_plane = np.zeros((height, width), dtype=np.uint8)

    # U and V planes (black = 128 in UV planes)
    # Both are quarter size of original due to 4:2:0 subsampling
    u_plane = np.full((height // 2, width // 2), 128, dtype=np.uint8)
    v_plane = np.full((height // 2, width // 2), 128, dtype=np.uint8)

    # Concatenate all planes
    yuv_frame = np.concatenate([y_plane.flatten(), u_plane.flatten(), v_plane.flatten()])

    return yuv_frame.astype(np.uint8).tobytes()


def half_ceil(x):
    return (x + 1) // 2


def scale_i420(frame, frame_size, new_size):
    """
    Scales an I420 (YUV 4:2:0) frame from 'frame_size' to 'new_size',
    handling odd frame widths/heights by using 'ceil' in the chroma planes.

    :param frame:      A bytes object containing the raw I420 frame data.
    :param frame_size: (orig_width, orig_height)
    :param new_size:   (new_width, new_height)
    :return:           A bytes object with the scaled I420 frame.
    """

    # 1) Unpack source / destination dimensions
    orig_width, orig_height = frame_size
    new_width, new_height = new_size

    # 2) Compute source plane sizes with rounding up for chroma
    orig_chroma_width = half_ceil(orig_width)
    orig_chroma_height = half_ceil(orig_height)

    y_plane_size = orig_width * orig_height
    uv_plane_size = orig_chroma_width * orig_chroma_height  # for each U or V

    # 3) Extract Y, U, V planes from the byte array
    y = np.frombuffer(frame[0:y_plane_size], dtype=np.uint8)
    u = np.frombuffer(frame[y_plane_size : y_plane_size + uv_plane_size], dtype=np.uint8)
    v = np.frombuffer(
        frame[y_plane_size + uv_plane_size : y_plane_size + 2 * uv_plane_size],
        dtype=np.uint8,
    )

    # 4) Reshape planes
    y = y.reshape(orig_height, orig_width)
    u = u.reshape(orig_chroma_height, orig_chroma_width)
    v = v.reshape(orig_chroma_height, orig_chroma_width)

    # ---------------------------------------------------------
    # Scale preserving aspect ratio or do letterbox/pillarbox
    # ---------------------------------------------------------
    input_aspect = orig_width / orig_height
    output_aspect = new_width / new_height

    if abs(input_aspect - output_aspect) < 1e-6:
        # Same aspect ratio; do a straightforward resize
        scaled_y = cv2.resize(y, (new_width, new_height), interpolation=cv2.INTER_LINEAR)

        # For U, V we should scale to half-dimensions (rounded up)
        # of the new size. But OpenCV requires exact (int) dims, so:
        target_u_width = half_ceil(new_width)
        target_u_height = half_ceil(new_height)

        scaled_u = cv2.resize(u, (target_u_width, target_u_height), interpolation=cv2.INTER_LINEAR)
        scaled_v = cv2.resize(v, (target_u_width, target_u_height), interpolation=cv2.INTER_LINEAR)

        # Flatten and return
        return np.concatenate([scaled_y.flatten(), scaled_u.flatten(), scaled_v.flatten()]).astype(np.uint8).tobytes()

    # Otherwise, the aspect ratios differ => letterbox or pillarbox
    if input_aspect > output_aspect:
        # The image is relatively wider => match width, shrink height
        scaled_width = new_width
        scaled_height = int(round(new_width / input_aspect))
    else:
        # The image is relatively taller => match height, shrink width
        scaled_height = new_height
        scaled_width = int(round(new_height * input_aspect))

    # 5) Resize Y, U, and V to the scaled dimensions
    scaled_y = cv2.resize(y, (scaled_width, scaled_height), interpolation=cv2.INTER_LINEAR)

    # For U, V, use half-dimensions of the scaled result, rounding up.
    scaled_u_width = half_ceil(scaled_width)
    scaled_u_height = half_ceil(scaled_height)
    scaled_u = cv2.resize(u, (scaled_u_width, scaled_u_height), interpolation=cv2.INTER_LINEAR)
    scaled_v = cv2.resize(v, (scaled_u_width, scaled_u_height), interpolation=cv2.INTER_LINEAR)

    # 6) Create the output buffers. For "dark" black:
    #    Y=0, U=128, V=128.
    final_y = np.zeros((new_height, new_width), dtype=np.uint8)
    final_u = np.full((half_ceil(new_height), half_ceil(new_width)), 128, dtype=np.uint8)
    final_v = np.full((half_ceil(new_height), half_ceil(new_width)), 128, dtype=np.uint8)

    # 7) Compute centering offsets for each plane (Y first)
    offset_y = (new_height - scaled_height) // 2
    offset_x = (new_width - scaled_width) // 2

    final_y[offset_y : offset_y + scaled_height, offset_x : offset_x + scaled_width] = scaled_y

    # Offsets for U and V planes are half of the Y offsets (integer floor)
    offset_y_uv = offset_y // 2
    offset_x_uv = offset_x // 2

    final_u[
        offset_y_uv : offset_y_uv + scaled_u_height,
        offset_x_uv : offset_x_uv + scaled_u_width,
    ] = scaled_u
    final_v[
        offset_y_uv : offset_y_uv + scaled_u_height,
        offset_x_uv : offset_x_uv + scaled_u_width,
    ] = scaled_v

    # 8) Flatten back to I420 layout and return bytes

    return np.concatenate([final_y.flatten(), final_u.flatten(), final_v.flatten()]).astype(np.uint8).tobytes()


def png_to_yuv420_frame(png_bytes: bytes) -> tuple:
    """
    Convert PNG image bytes to YUV420 (I420) format without resizing,
    and return the dimensions of the resulting image. The conversion does not work unless the
    image dimensions are even, so the image is cropped slightly to make the dimensions even.

    Args:
        png_bytes (bytes): Input PNG image as bytes

    Returns:
        tuple: (YUV420 formatted frame data, width, height)
    """
    # Convert PNG bytes to numpy array
    png_array = np.frombuffer(png_bytes, np.uint8)
    bgr_frame = cv2.imdecode(png_array, cv2.IMREAD_COLOR)

    # Get original dimensions
    height, width = bgr_frame.shape[:2]

    # If dimensions are 1, add padding to make them 2
    if height == 1:
        bgr_frame = cv2.copyMakeBorder(bgr_frame, 0, 1, 0, 0, cv2.BORDER_REPLICATE)
        height += 1
    if width == 1:
        bgr_frame = cv2.copyMakeBorder(bgr_frame, 0, 0, 0, 1, cv2.BORDER_REPLICATE)
        width += 1

    # Ensure even dimensions by cropping if necessary
    if width % 2 != 0:
        bgr_frame = bgr_frame[:, :-1]
        width -= 1
    if height % 2 != 0:
        bgr_frame = bgr_frame[:-1, :]
        height -= 1

    # Convert BGR to YUV420 (I420)
    yuv_frame = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2YUV_I420)

    # Return frame data and dimensions
    return yuv_frame.tobytes(), width, height


def utterance_words(utterance, offset=0.0):
    if "words" in utterance.transcription:
        return utterance.transcription["words"]

    return [
        {
            "start": offset,
            "end": offset + utterance.duration_ms / 1000.0,
            "punctuated_word": utterance.transcription["transcript"],
            "word": utterance.transcription["transcript"],
        }
    ]


class AggregatedUtterance:
    def __init__(self, utterance):
        self.participant = utterance.participant
        self.transcription = utterance.transcription.copy()
        self.timestamp_ms = utterance.timestamp_ms
        self.duration_ms = utterance.duration_ms
        self.id = utterance.id
        self.transcription["words"] = utterance_words(utterance)

    def aggregate(self, utterance):
        self.transcription["words"].extend(utterance_words(utterance, offset=(utterance.timestamp_ms - self.timestamp_ms) / 1000.0))
        self.transcription["transcript"] += " " + utterance.transcription["transcript"]
        self.duration_ms += utterance.duration_ms


def generate_aggregated_utterances(recording):
    utterances_sorted = sorted(recording.utterances.filter(async_transcription=None).all(), key=lambda x: x.timestamp_ms)

    aggregated_utterances = []
    current_aggregated_utterance = None
    for utterance in utterances_sorted:
        if not utterance.transcription:
            continue
        if not utterance.transcription.get("transcript"):
            continue

        if current_aggregated_utterance is None:
            current_aggregated_utterance = AggregatedUtterance(utterance)
        else:
            if utterance.transcription.get("words") is None and utterance.participant.id == current_aggregated_utterance.participant.id and utterance.timestamp_ms - (current_aggregated_utterance.timestamp_ms + current_aggregated_utterance.duration_ms) < 3000:
                current_aggregated_utterance.aggregate(utterance)
            else:
                aggregated_utterances.append(current_aggregated_utterance)
                current_aggregated_utterance = AggregatedUtterance(utterance)

    if current_aggregated_utterance:
        aggregated_utterances.append(current_aggregated_utterance)
    return aggregated_utterances


def generate_failed_utterance_json_for_bot_detail_view(recording):
    failed_utterances = recording.utterances.filter(async_transcription=None).filter(failure_data__isnull=False).order_by("timestamp_ms")[:10]

    failed_utterances_data = []

    for utterance in failed_utterances:
        utterance_data = {
            "id": utterance.id,
            "failure_data": utterance.failure_data,
        }
        failed_utterances_data.append(utterance_data)

    return failed_utterances_data


def generate_utterance_json_for_bot_detail_view(recording):
    utterances_data = []
    recording_first_buffer_timestamp_ms = recording.first_buffer_timestamp_ms

    aggregated_utterances = generate_aggregated_utterances(recording)
    for utterance in aggregated_utterances:
        if not utterance.transcription:
            continue
        if not utterance.transcription.get("transcript"):
            continue

        if recording_first_buffer_timestamp_ms:
            if utterance.transcription.get("words"):
                first_word_start_relative_ms = int(utterance.transcription.get("words")[0].get("start") * 1000)
            else:
                first_word_start_relative_ms = 0

            relative_timestamp_ms = utterance.timestamp_ms - recording_first_buffer_timestamp_ms + first_word_start_relative_ms
        else:
            # If we don't have a first buffer timestamp, we don't have a relative timestamp
            relative_timestamp_ms = None

        relative_words_data = []
        if utterance.transcription.get("words"):
            if recording_first_buffer_timestamp_ms:
                utterance_start_relative_ms = utterance.timestamp_ms - recording_first_buffer_timestamp_ms
            else:
                # If we don't have a first buffer timestamp, we use the absolute timestamp
                utterance_start_relative_ms = utterance.timestamp_ms

            for word in utterance.transcription["words"]:
                relative_word = word.copy()
                relative_word["start"] = utterance_start_relative_ms + int(word["start"] * 1000)
                relative_word["end"] = utterance_start_relative_ms + int(word["end"] * 1000)
                relative_words_data.append(relative_word)

        relative_words_data_with_spaces = []
        for i, word in enumerate(relative_words_data):
            relative_words_data_with_spaces.append(
                {
                    "word": word.get("punctuated_word") or word.get("word"),
                    "start": word["start"],
                    "end": word["end"],
                    "utterance_id": utterance.id,
                }
            )
            # Add space between words
            if i < len(relative_words_data) - 1:
                next_word = relative_words_data[i + 1]
                relative_words_data_with_spaces.append(
                    {
                        "word": " ",
                        "start": next_word["start"],
                        "end": next_word["start"],
                        "utterance_id": utterance.id,
                        "is_space": True,
                    }
                )

        timestamp_display = None
        if relative_timestamp_ms is not None:
            seconds = relative_timestamp_ms // 1000
            timestamp_display = f"{seconds // 60}:{seconds % 60:02d}"

        utterance_data = {
            "id": utterance.id,
            "participant": utterance.participant,
            "relative_timestamp_ms": relative_timestamp_ms,
            "words": relative_words_data_with_spaces,
            "transcript": utterance.transcription.get("transcript"),
            "timestamp_display": timestamp_display,
        }
        utterances_data.append(utterance_data)

    return utterances_data


def transcription_provider_from_bot_creation_data(data):
    url = data.get("meeting_url")
    settings = data.get("transcription_settings", {})
    use_zoom_web_adapter = data.get("zoom_settings", {}).get("sdk") == "web"

    if "deepgram" in settings:
        return TranscriptionProviders.DEEPGRAM
    elif "gladia" in settings:
        return TranscriptionProviders.GLADIA
    elif "openai" in settings:
        return TranscriptionProviders.OPENAI
    elif "assembly_ai" in settings:
        return TranscriptionProviders.ASSEMBLY_AI
    elif "sarvam" in settings:
        return TranscriptionProviders.SARVAM
    elif "elevenlabs" in settings:
        return TranscriptionProviders.ELEVENLABS
    elif "meeting_closed_captions" in settings:
        return TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM

    # Return default provider. Which is deepgram for Zoom, and meeting_closed_captions for Google Meet / Teams
    if meeting_type_from_url(url) == MeetingTypes.ZOOM and not use_zoom_web_adapter:
        return TranscriptionProviders.DEEPGRAM
    return TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM


def generate_recordings_json_for_bot_detail_view(bot):
    # Process recordings and utterances
    recordings_data = []
    for recording in bot.recordings.all():
        recordings_data.append(
            {
                "state": recording.state,
                "recording_type": recording.bot.recording_type(),
                "transcription_state": recording.transcription_state,
                "url": recording.url,
                "utterances": generate_utterance_json_for_bot_detail_view(recording),
                "failed_utterances": generate_failed_utterance_json_for_bot_detail_view(recording),
            }
        )

    return recordings_data


def is_valid_png(image_data: bytes) -> bool:
    """
    Validates whether the provided bytes data is a valid PNG image.

    Args:
        image_data (bytes): The image data to validate

    Returns:
        bool: True if the data is a valid PNG image, False otherwise
    """
    try:
        # First check for the PNG signature (first 8 bytes)
        png_signature = b"\x89PNG\r\n\x1a\n"
        if not image_data.startswith(png_signature):
            return False

        # Try to decode the image using OpenCV
        img_array = np.frombuffer(image_data, np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

        # If img is None, the decoding failed
        return img is not None
    except Exception:
        return False
