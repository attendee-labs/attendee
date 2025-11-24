import asyncio
import audioop
import logging
import queue
import re
import threading
import time

import msgpack
import numpy as np
import websockets

logger = logging.getLogger(__name__)

# Global callback queue - all speakers enqueue callbacks here
# A single consumer thread processes them sequentially to ensure DB order
_callback_queue = queue.Queue()
_callback_consumer_thread = None
_callback_consumer_running = False
_callback_consumer_lock = threading.Lock()


def _ensure_callback_consumer_started():
    """Ensure the callback consumer thread is running (lazy initialization)."""
    global _callback_consumer_thread, _callback_consumer_running

    # Double-checked locking pattern for thread-safe lazy init
    if _callback_consumer_running:
        return

    with _callback_consumer_lock:
        # Check again inside lock
        if _callback_consumer_running:
            return

        _callback_consumer_running = True

        def consume_callbacks():
            """Process callbacks from queue sequentially."""
            logger.info("Kyutai callback consumer thread started")
            while _callback_consumer_running:
                try:
                    # Wait for callback with timeout to allow graceful shutdown
                    callback_func = _callback_queue.get(timeout=1.0)

                    try:
                        # Execute callback - this writes to DB
                        callback_func()
                    except Exception as e:
                        logger.error(f"Error executing callback: {e}", exc_info=True)
                    finally:
                        _callback_queue.task_done()

                except queue.Empty:
                    # Timeout - check if we should keep running
                    continue

        _callback_consumer_thread = threading.Thread(target=consume_callbacks, daemon=True, name="kyutai-callback-consumer")
        _callback_consumer_thread.start()
        logger.info("Kyutai callback consumer thread initialized")


# Kyutai server expects audio at exactly 24000 Hz
KYUTAI_SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 1  # mono
FRAME_SIZE = 1920  # Fixed frame size for sending (80ms at 24kHz)

# Kyutai's semantic VAD has multiple prediction heads for different pause lengths
# Index 0: 0.5s, Index 1: 1.0s, Index 2: 2.0s, Index 3: 3.0s
# We use 0.5 seconds as a good balance for natural speech segmentation
PAUSE_PREDICTION_HEAD_INDEX = 0
PAUSE_THRESHOLD = 0.25  # Confidence threshold for detecting pauses


def _sanitize_text(text):
    """
    Sanitize text by removing invalid/problematic characters.

    Handles cases where Kyutai server sends:
    - Invalid UTF-8 sequences (� replacement characters)
    - Control characters
    - Other problematic Unicode characters

    Returns cleaned text or None if text becomes empty.
    """
    if not text:
        return None

    # Remove replacement character (�) and other problematic characters
    # U+FFFD is the replacement character
    text = text.replace("\ufffd", "")

    # Remove control characters except newline, tab, carriage return
    text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]", "", text)

    # Strip whitespace
    text = text.strip()

    # Return None if empty after cleaning
    return text if text else None


class KyutaiStreamingTranscriber:
    """
    Buffered streaming transcriber for Kyutai STT service.

    This class handles real-time speech-to-text transcription using
    the Kyutai service with audio buffering for fixed-size frames.

    Key improvements over the original version:
    - Audio buffering to send fixed-size frames (FRAME_SIZE samples)
    - Separate sender/receiver tasks for better concurrency
    - Frame-based timing control for accurate playback speed
    - Thread-safe queue operations using call_soon_threadsafe
    """

    def __init__(
        self,
        *,
        server_url,
        sample_rate,
        metadata=None,
        interim_results=True,
        api_key=None,
        save_utterance_callback=None,
        max_retry_time=300,
        debug_logging=False,
    ):
        """
        Initialize the Kyutai streaming transcriber.

        Args:
            server_url: URL of the Kyutai server
                (e.g., "ws://localhost:8080")
            sample_rate: Audio sample rate (Kyutai uses 24000 Hz)
            metadata: Optional metadata to send with the connection
            interim_results: Whether to receive interim results
            api_key: API key for authentication (optional)
            save_utterance_callback: Callback function for saving utterances
                      (receives transcript text)
            max_retry_time: Maximum time in seconds to keep retrying
                connection (default: 300s / 5 minutes)
            debug_logging: Enable verbose debug logging for every message
                (default: False, logs only important events)
        """
        self.server_url = server_url
        self.sample_rate = sample_rate
        self.metadata = metadata or {}
        self.interim_results = interim_results
        self.api_key = api_key
        self.save_utterance_callback = save_utterance_callback
        self.max_retry_time = max_retry_time
        self.debug_logging = debug_logging

        # Performance optimization: Cache resampling state
        self._resampler_state = None

        # Audio buffer for accumulating samples to FRAME_SIZE
        # Use numpy array for efficient concatenation and slicing
        self._audio_buffer = np.array([], dtype=np.float32)

        # Extract participant name from metadata for better logging
        # Metadata uses "participant_full_name" key from adapter
        self._participant_name = metadata.get("participant_full_name", "Unknown") if metadata else "Unknown"

        # Audio send queue for buffered transmission
        self._send_queue = None  # Will be created in event loop
        self._sender_task = None
        self._receiver_task = None
        self._ws_connection = None

        # Timing for frame-based sending
        self._start_time = None
        self._frame_count = 0

        # Event loop management - run asyncio in background thread
        self._loop = None
        self._loop_thread = None
        self._connect_future = None

        # Track current transcript
        self.current_transcript = []
        # Audio stream anchor: wall-clock time when server sent "Ready"
        # This is the stable reference point for all timestamp calculations
        self.audio_stream_anchor_time = None
        # Track when last word was received (wall clock, for silence detection)
        self.last_word_received_time = None
        # Track problematic character occurrences for health monitoring
        self.invalid_text_count = 0
        self.last_valid_word_time = None
        # Track audio stream positions for current utterance
        self.current_utterance_first_word_start_time = None  # From "Word"
        self.current_utterance_last_word_stop_time = None  # From "EndWord"

        # Semantic VAD tracking (from Step messages)
        self.semantic_vad_detected_pause = False
        self.speech_started = False  # Track if we've received any words

        # Rate limiting for utterance emission checks
        self._last_utterance_check_time = 0.0
        self._utterance_check_interval = 0.1  # Check at most every 100ms

        # Track when audio was last sent (used for monitoring/cleanup)
        self.last_send_time = time.time()

        # WebSocket connection state
        self.ws = None
        self.connected = False  # True when WebSocket is actively connected
        self.should_stop = False  # True when finish() is called (intentional shutdown)
        self.reconnecting = True  # Start as True since we begin connecting immediately

        # Start event loop in background thread and initialize connection
        self._start_event_loop()

    def _start_event_loop(self):
        """Start asyncio event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_event_loop, daemon=True, name="kyutai-event-loop")
        self._loop_thread.start()

        # Wait for loop to start
        time.sleep(0.1)

        # Schedule connection in the loop
        self._connect_future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)

    def _run_event_loop(self):
        """Run the event loop in background thread."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _connect(self):
        """
        Establish WebSocket connection to Kyutai server with retry logic.

        Uses exponential backoff (1s, 2s, 4s, 8s, 16s) followed by
        fixed 10-second intervals until connection succeeds or max_retry_time
        is reached.
        """
        # Exponential backoff delays (in seconds)
        exponential_delays = [1, 2, 4, 8, 16]
        # Fixed delay after exponential backoff exhausted
        fixed_delay = 10

        attempt = 0
        start_time = time.time()

        while not self.should_stop:
            elapsed_time = time.time() - start_time

            # Check if we've exceeded max retry time
            if elapsed_time >= self.max_retry_time:
                logger.error(f"Failed to connect to Kyutai server after {self.max_retry_time}s. Giving up.")
                return

            try:
                attempt += 1
                logger.info(f"[{self._participant_name}] Attempting to connect to Kyutai server (attempt {attempt}, elapsed: {elapsed_time:.1f}s)")

                # Add authentication header if API key is provided
                additional_headers = {}
                if self.api_key:
                    additional_headers["kyutai-api-key"] = self.api_key

                # Connect with websockets library (async!)
                async with websockets.connect(
                    self.server_url,
                    additional_headers=additional_headers,
                    ping_interval=20,  # Send ping every 20 seconds
                    ping_timeout=10,  # Timeout if no pong within 10 seconds
                ) as ws:
                    self._ws_connection = ws
                    self.connected = True
                    self.reconnecting = False  # Successfully connected

                    # Create send queue in the event loop
                    self._send_queue = asyncio.Queue()

                    logger.info(f"✅ [{self._participant_name}] Successfully connected to Kyutai server after {attempt} attempt(s)")

                    # Start both receiver and sender tasks
                    self._receiver_task = asyncio.create_task(self._receiver_loop())
                    self._sender_task = asyncio.create_task(self._sender_loop())

                    # Wait for both tasks
                    await asyncio.gather(self._receiver_task, self._sender_task, return_exceptions=True)

                # Connection closed - check if intentional
                if self.should_stop:
                    logger.info(f"[{self._participant_name}] Kyutai connection closed (shutdown)")
                    return

                logger.warning(f"[{self._participant_name}] Kyutai connection closed unexpectedly, will retry...")
                # Mark as reconnecting since we'll retry
                self.reconnecting = True

            except asyncio.CancelledError:
                logger.info(f"[{self._participant_name}] Kyutai connection cancelled")
                return
            except Exception as e:
                logger.error(f"[{self._participant_name}] Error connecting to Kyutai server (attempt {attempt}): {e}")
                # Mark as reconnecting since we'll retry
                self.reconnecting = True

            # Connection failed, determine retry delay
            if attempt <= len(exponential_delays):
                # Use exponential backoff
                delay = exponential_delays[attempt - 1]
                logger.warning(f"[{self._participant_name}] Retrying in {delay}s (exponential backoff)...")
            else:
                # Use fixed delay
                delay = fixed_delay
                logger.warning(f"[{self._participant_name}] Retrying in {delay}s...")

            # Check if delay would exceed max retry time
            if elapsed_time + delay > self.max_retry_time:
                remaining_time = self.max_retry_time - elapsed_time
                if remaining_time > 0:
                    logger.info(f"[{self._participant_name}] Only {remaining_time:.1f}s remaining before timeout")
                    await asyncio.sleep(remaining_time)
                # Gave up - stop reconnecting
                self.reconnecting = False
                break
            else:
                await asyncio.sleep(delay)

            # Reset connection state for retry
            self.connected = False
            self._ws_connection = None
            self._send_queue = None

        # Exited retry loop - mark as not reconnecting
        self.reconnecting = False

    async def _receiver_loop(self):
        """
        Async receiver loop - processes messages from WebSocket.
        """
        try:
            async for message in self._ws_connection:
                await self._process_message(message)
        except websockets.exceptions.ConnectionClosed as e:
            if not self.should_stop:
                logger.warning(f"[{self._participant_name}] Kyutai WebSocket connection closed unexpectedly: {e}")
            else:
                logger.info(f"[{self._participant_name}] Kyutai WebSocket connection closed normally")
        except Exception as e:
            if not self.should_stop:
                logger.error(f"[{self._participant_name}] Error in receiver loop: {e}", exc_info=True)
        finally:
            self.connected = False

    async def _sender_loop(self):
        """Send messages from queue with frame-based timing control."""
        try:
            while not self.should_stop:
                try:
                    message = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)

                    if not self.connected or not self._ws_connection:
                        continue

                    # Initialize start time on first message
                    if self._start_time is None:
                        self._start_time = time.time()

                    # Increment frame counter
                    self._frame_count += 1

                    # Calculate expected send time based on frame count
                    # Using 1.0 as playback_speed for real-time transcription
                    expected_send_time = self._start_time + (self._frame_count * FRAME_SIZE) / KYUTAI_SAMPLE_RATE
                    current_time = time.time()

                    # Sleep if we're ahead of schedule
                    if current_time < expected_send_time:
                        await asyncio.sleep(expected_send_time - current_time)

                    # Send message
                    await self._ws_connection.send(message)

                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self._participant_name}] Sender error: {e}", exc_info=True)

    async def _process_message(self, message):
        """
        Handle incoming transcription messages from Kyutai server.

        Expected message format (MessagePack):
        - {"type": "Word", "text": "word", "start_time": 0.0}
        - {"type": "EndWord", "stop_time": 0.5}
        - {"type": "Step", ...}
        - {"type": "Marker"}
        """
        try:
            # Decode MessagePack message
            # Use strict_map_key=False to handle diverse key types
            # raw=False decodes bytes to str (UTF-8)
            try:
                data = msgpack.unpackb(message, raw=False, strict_map_key=False)
            except (UnicodeDecodeError, ValueError) as decode_err:
                # Handle encoding errors gracefully
                logger.error(f"[{self._participant_name}] Kyutai: Failed to decode message: {decode_err}. Attempting recovery with error handling...")
                # Try again with raw=True, manually decode with error handling
                try:
                    data = msgpack.unpackb(message, raw=True)
                    # Manually decode text fields with error handling
                    if isinstance(data.get(b"text"), bytes):
                        data[b"text"] = data[b"text"].decode("utf-8", errors="replace")
                    if isinstance(data.get(b"type"), bytes):
                        data[b"type"] = data[b"type"].decode("utf-8")
                    # Convert byte keys to strings
                    data = {k.decode("utf-8") if isinstance(k, bytes) else k: v for k, v in data.items()}
                except Exception as recovery_err:
                    logger.error(f"[{self._participant_name}] Kyutai: Recovery failed: {recovery_err}. Skipping message.")
                    return

            msg_type = data.get("type")

            if msg_type == "Word":
                # Received a new word
                raw_text = data.get("text", "")
                start_time = data.get("start_time", 0.0)

                # Sanitize text to handle encoding issues
                text = _sanitize_text(raw_text)

                # Log if we received problematic characters
                if raw_text and not text:
                    self.invalid_text_count += 1
                    logger.warning(f"[{self._participant_name}] Kyutai: Filtered out invalid text at {start_time:.2f}s (raw bytes: {raw_text.encode('utf-8', errors='replace')}) [{self.invalid_text_count} invalid texts so far]")
                elif raw_text != text:
                    self.invalid_text_count += 1
                    logger.warning(f"[{self._participant_name}] Kyutai: Sanitized text from '{raw_text}' to '{text}' at {start_time:.2f}s [{self.invalid_text_count} invalid texts so far]")

                # Debug logging only (verbose)
                if self.debug_logging and text:
                    wall_clock_now = time.time()
                    audio_offset = None
                    if self.audio_stream_anchor_time is not None:
                        audio_offset = wall_clock_now - self.audio_stream_anchor_time
                    logger.debug(f"[{self._participant_name}] Kyutai Word: '{text}' start={start_time:.4f}s offset={audio_offset:.4f}s transcript_len={len(self.current_transcript)}")

                if text:
                    # Track valid word reception
                    self.last_valid_word_time = time.time()

                    # Check for significant gap - emit previous utterance
                    if self.current_transcript and self.current_utterance_last_word_stop_time is not None and start_time - self.current_utterance_last_word_stop_time > 1.0:
                        if self.debug_logging:
                            gap = start_time - self.current_utterance_last_word_stop_time
                            logger.debug(f"[{self._participant_name}] Kyutai: {gap:.2f}s silence, emitting utterance")
                        self._emit_current_utterance()

                    # Track first word's start_time for this utterance
                    if not self.current_transcript:
                        self.current_utterance_first_word_start_time = start_time

                    # Track when this word was received (wall clock)
                    self.last_word_received_time = time.time()

                    # Mark that speech has started (for semantic VAD)
                    self.speech_started = True

                    # Add to current transcript
                    self.current_transcript.append({"text": text, "timestamp": [start_time, start_time]})

            elif msg_type == "EndWord":
                # Update the end time of the last word
                stop_time = data.get("stop_time", 0.0)
                if self.current_transcript:
                    # Update timestamp efficiently
                    self.current_transcript[-1]["timestamp"][1] = stop_time

                    # Track the last word's stop time for utterance
                    self.current_utterance_last_word_stop_time = stop_time

                    # Debug logging only
                    if self.debug_logging:
                        word_data = self.current_transcript[-1]
                        logger.debug(f"[{self._participant_name}] Kyutai EndWord: '{word_data['text']}' [{word_data['timestamp'][0]:.2f}s - {word_data['timestamp'][1]:.2f}s]")

            elif msg_type == "Step":
                # Step messages contain semantic VAD predictions
                # The "prs" field contains pause predictions
                # for different lengths
                if "prs" in data and len(data["prs"]) > PAUSE_PREDICTION_HEAD_INDEX:
                    pause_prediction = data["prs"][PAUSE_PREDICTION_HEAD_INDEX]

                    # Detect pause: high confidence prediction
                    # + speech has started
                    if pause_prediction > PAUSE_THRESHOLD and self.speech_started:
                        self.semantic_vad_detected_pause = True
                        # Emit utterance on natural pause
                        self._check_and_emit_utterance()

            elif msg_type == "Marker":
                # End of stream marker received
                logger.info(f"[{self._participant_name}] Kyutai: End of stream marker received")
                # Emit any remaining transcript
                self._emit_current_utterance()

            elif msg_type == "Ready":
                # Server is ready - set our time anchor for timestamp
                # calculations
                # All audio timestamps will be relative to this moment
                self.audio_stream_anchor_time = time.time()
                logger.info(f"🎯 [{self._participant_name}] Kyutai: Audio stream anchor set (Ready signal)")

            else:
                logger.warning(f"[{self._participant_name}] Unknown Kyutai message type: {msg_type}")

        except Exception as e:
            logger.error(f"[{self._participant_name}] Error processing Kyutai message: {e}")
            logger.debug(f"Raw message: {message}")

    def send(self, audio_data):
        """
        Send audio data to the Kyutai server with buffering.

        Audio is accumulated into fixed-size frames (FRAME_SIZE samples)
        before being queued for transmission. This ensures consistent
        message sizes and timing.

        Args:
            audio_data: Audio data as bytes (int16 PCM)

        Raises:
            ConnectionError: If connection failed permanently (gave up reconnecting)
        """
        # If not connected and not reconnecting and not stopping, connection failed permanently
        if not self.connected and not self.reconnecting and not self.should_stop:
            raise ConnectionError("Kyutai WebSocket connection failed permanently")

        if not self.connected or self.should_stop:
            # Silently drop audio during shutdown, reconnection, or when disconnected
            return

        # Update last send time for monitoring/cleanup
        self.last_send_time = time.time()

        try:
            # Resample if needed (cache resampler state for performance)
            if self.sample_rate != KYUTAI_SAMPLE_RATE:
                audio_data, self._resampler_state = audioop.ratecv(
                    audio_data,
                    SAMPLE_WIDTH,
                    CHANNELS,
                    self.sample_rate,
                    KYUTAI_SAMPLE_RATE,
                    self._resampler_state,
                )

            # Convert int16 bytes to float32 in one operation
            # np.frombuffer is zero-copy, astype creates new array
            audio_samples = np.frombuffer(audio_data, dtype=np.int16)
            audio_float = audio_samples.astype(np.float32) / 32768.0

            # Add to buffer using numpy concatenation (efficient)
            self._audio_buffer = np.concatenate([self._audio_buffer, audio_float])

            # Send frames of FRAME_SIZE
            while len(self._audio_buffer) >= FRAME_SIZE:
                # Extract one frame (numpy slicing is efficient)
                frame = self._audio_buffer[:FRAME_SIZE]
                self._audio_buffer = self._audio_buffer[FRAME_SIZE:]

                # Pack message - convert numpy array to list for msgpack
                message = msgpack.packb(
                    {"type": "Audio", "pcm": frame.tolist()},
                    use_bin_type=True,
                    use_single_float=True,
                )

                # Queue for sending with timing in sender loop
                # Use call_soon_threadsafe for thread-safe queue operations
                if self._loop and self._send_queue:
                    try:
                        self._loop.call_soon_threadsafe(self._send_queue.put_nowait, message)
                    except Exception as queue_error:
                        # Queue full or loop closed - connection likely dead
                        logger.warning(f"[{self._participant_name}] Failed to queue audio, connection may be dead: {queue_error}")
                        self.connected = False
                        break

        except Exception as e:
            logger.error(f"[{self._participant_name}] Error sending audio to Kyutai: {e}", exc_info=True)
            # Mark as disconnected so it can be recreated
            self.connected = False

    async def _flush_buffer(self):
        """Flush remaining audio in buffer (may be smaller than FRAME_SIZE)."""
        if len(self._audio_buffer) > 0 and self._send_queue:
            logger.debug(f"[{self._participant_name}] Flushing {len(self._audio_buffer)} buffered samples")

            # Send remaining samples (convert numpy array to list)
            message = msgpack.packb(
                {"type": "Audio", "pcm": self._audio_buffer.tolist()},
                use_bin_type=True,
                use_single_float=True,
            )
            await self._send_queue.put(message)
            self._audio_buffer = np.array([], dtype=np.float32)

    def _check_and_emit_utterance(self):
        """
        Check if there's a natural pause in speech to emit utterance.
        Uses semantic VAD from Kyutai when available, falls back to timing.
        Rate-limited to avoid excessive webhook calls.
        """
        if not self.current_transcript:
            return

        # Check if we've received any words yet
        if self.last_word_received_time is None:
            return

        # Priority 1: Semantic VAD detected a natural pause
        if self.semantic_vad_detected_pause:
            # Emit utterance when semantic VAD detects pause
            # The semantic VAD is trained to detect natural speech boundaries,
            # so we trust it even for shorter utterances (3+ words)
            word_count = len(self.current_transcript)

            # Calculate utterance duration if possible
            utterance_duration = 0
            if self.current_utterance_first_word_start_time is not None and self.current_utterance_last_word_stop_time is not None:
                utterance_duration = self.current_utterance_last_word_stop_time - self.current_utterance_first_word_start_time

            # Emit if meets minimum quality criteria:
            # - At least 3 words (catches short complete phrases)
            # - OR more than 1.5 seconds of speech
            if word_count >= 3 or utterance_duration > 1.5:
                logger.info(f"Kyutai [{self._participant_name}]: Emitting utterance on semantic VAD pause ({word_count} words, {utterance_duration:.1f}s)")
                self._emit_current_utterance()
                self.semantic_vad_detected_pause = False  # Reset flag
                return

            # Very short utterance (1-2 words) - check time since pause detected
            # If we detected the pause more than 0.5s ago, emit anyway
            current_time = time.time()
            if self.last_word_received_time is not None:
                time_since_last_word = current_time - self.last_word_received_time
                if time_since_last_word > 0.5:
                    logger.info(f"Kyutai [{self._participant_name}]: Emitting short utterance after pause+delay ({word_count} words, delay={time_since_last_word:.2f}s)")
                    self._emit_current_utterance()
                    self.semantic_vad_detected_pause = False
                    return

            # Still very fresh - wait a bit longer
            logger.debug(f"Kyutai [{self._participant_name}]: Delaying emission - very short utterance ({word_count} words, {utterance_duration:.1f}s)")
            # Keep flag set, will check again soon
            return

        # Rate limiting: Don't check too frequently (causes webhook spam)
        current_time = time.time()
        time_since_last_check = current_time - self._last_utterance_check_time
        if time_since_last_check < self._utterance_check_interval:
            return  # Skip this check, too soon

        self._last_utterance_check_time = current_time

        # Priority 2: Time-based silence detection (fallback)
        silence_duration = current_time - self.last_word_received_time

        # Require minimum silence before emitting to avoid fragmentation
        MIN_SILENCE_FOR_EMIT = 0.8  # 800ms minimum silence

        # For single-word utterances, be more patient waiting for EndWord
        if len(self.current_transcript) == 1:
            # Wait up to 1.5s for EndWord on single-word utterances
            if self.current_utterance_last_word_stop_time is None:
                if silence_duration > 1.5:
                    logger.info(f"Kyutai [{self._participant_name}]: Single-word utterance, no EndWord after {silence_duration:.2f}s - emitting anyway")
                    self._emit_current_utterance()
            else:
                # Have EndWord, can emit after minimum silence
                if silence_duration > MIN_SILENCE_FOR_EMIT:
                    self._emit_current_utterance()
        else:
            # Multi-word utterance: emit after minimum silence
            if silence_duration > MIN_SILENCE_FOR_EMIT:
                self._emit_current_utterance()

    def _emit_current_utterance(self):
        """Emit the current transcript as an utterance and clear it."""
        if self.current_transcript and self.save_utterance_callback:
            # Convert list of word objects to text efficiently
            transcript_text = " ".join([w["text"] for w in self.current_transcript])

            # Calculate timestamp and duration using audio stream positions
            if self.audio_stream_anchor_time is not None and self.current_utterance_first_word_start_time is not None:
                # Timestamp: When utterance started in wall-clock time
                timestamp_ms = int((self.audio_stream_anchor_time + self.current_utterance_first_word_start_time) * 1000)

                # Duration: Speaking duration from first to last word
                if self.current_utterance_last_word_stop_time is not None:
                    # Have EndWord timing - use it
                    duration_seconds = self.current_utterance_last_word_stop_time - self.current_utterance_first_word_start_time
                    duration_ms = int(duration_seconds * 1000)
                else:
                    # EndWord not received - estimate minimum duration
                    # Use elapsed time since word started as a minimum estimate
                    current_time = time.time()
                    elapsed_since_utterance_start = current_time - (self.audio_stream_anchor_time + self.current_utterance_first_word_start_time)

                    # For multi-word utterances, use last word's start time if available
                    if len(self.current_transcript) > 1 and self.current_transcript:
                        last_word_start = self.current_transcript[-1]["timestamp"][0]
                        duration_from_timestamps = last_word_start - self.current_utterance_first_word_start_time
                        # Use the larger of: timestamp-based duration or elapsed time estimate
                        duration_seconds = max(duration_from_timestamps, elapsed_since_utterance_start)
                    else:
                        # Single word - use elapsed time since word started
                        duration_seconds = elapsed_since_utterance_start

                    duration_ms = max(int(duration_seconds * 1000), 1)  # Ensure at least 1ms
            else:
                # Fallback if we don't have proper anchoring
                if self.debug_logging:
                    logger.warning(f"[{self._participant_name}] Kyutai: Missing timing anchors")
                timestamp_ms = int(time.time() * 1000)
                duration_ms = 0

            # Always log emitted utterances (important for monitoring)
            logger.debug(
                f"Kyutai [{self._participant_name}]: Emitting utterance [{duration_ms}ms, {len(self.current_transcript)} words]: {transcript_text[:100]}"  # Truncate long utterances
            )

            # Call callback with duration and timestamp in metadata
            metadata = {
                "duration_ms": duration_ms,
                "timestamp_ms": timestamp_ms,
            }

            # Enqueue callback to global queue for sequential processing
            # All speakers share one queue, processed by single consumer thread
            # This ensures DB writes happen in chronological order
            def run_callback():
                try:
                    self.save_utterance_callback(transcript_text, metadata)
                except Exception as e:
                    logger.error(f"Error in save_utterance_callback: {e}", exc_info=True)

            # Ensure consumer thread is running (lazy init for Celery workers)
            _ensure_callback_consumer_started()

            # Add to queue - consumer thread will process sequentially
            _callback_queue.put(run_callback)

            # Clear transcript for next utterance
            self.current_transcript = []
            # Reset timing for next utterance
            self.current_utterance_first_word_start_time = None
            self.current_utterance_last_word_stop_time = None
            self.last_word_received_time = None
            # Reset semantic VAD state
            self.semantic_vad_detected_pause = False
            self.speech_started = False

    def finish(self):
        """
        Close the connection and clean up resources.
        Fast cleanup optimized for multi-speaker scenarios.
        """
        if self.should_stop:
            return  # Already finishing

        self.should_stop = True
        logger.info(f"Finishing Kyutai transcriber [{self._participant_name}]")

        # Emit any remaining transcript before closing
        self._emit_current_utterance()
        self.should_stop = True

        try:
            # Signal stop to async tasks
            if self._loop and self._loop.is_running():
                # Flush buffer and send Marker message to indicate end
                # of stream
                if self.connected and self._ws_connection:

                    async def flush_and_close():
                        try:
                            # Flush any remaining audio buffer
                            await self._flush_buffer()

                            # Send marker (fire and forget)
                            marker_msg = msgpack.packb({"type": "Marker", "id": 0}, use_bin_type=True)
                            await self._ws_connection.send(marker_msg)

                            # Close WebSocket immediately
                            await self._ws_connection.close()
                        except Exception as e:
                            logger.error(f"[{self._participant_name}] Error closing WebSocket: {e}")

                    # Schedule close but don't wait for it
                    asyncio.run_coroutine_threadsafe(flush_and_close(), self._loop)

                # Stop the event loop immediately (don't wait)
                self._loop.call_soon_threadsafe(self._loop.stop)

            # Don't wait for thread - let it finish in background
            # This releases the connection immediately for other speakers
            logger.info(f"Released connection [{self._participant_name}] (background cleanup)")

        except Exception as e:
            logger.error(f"Error finishing Kyutai transcriber [{self._participant_name}]: {e}")
        finally:
            self.connected = False
