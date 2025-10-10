import asyncio
import audioop
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import msgpack
import numpy as np
import websockets

logger = logging.getLogger(__name__)

# Kyutai server expects audio at exactly 24000 Hz
KYUTAI_SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 1  # mono

# Kyutai's semantic VAD
PAUSE_PREDICTION_HEAD_INDEX = 0
PAUSE_THRESHOLD = 0.5

# Batching configuration
BATCH_SIZE = 3  # Number of chunks to batch together
BATCH_TIMEOUT = 0.05  # Max time (seconds) to wait for batch


class KyutaiStreamingTranscriber:
    """
    Async streaming transcriber for Kyutai STT with batching.

    Reduces latency with multiple speakers by using async WebSocket
    and batching audio chunks to minimize overhead.
    """

    def __init__(
        self,
        *,
        server_url,
        sample_rate,
        metadata=None,
        interim_results=True,
        model=None,
        api_key=None,
        callback=None,
    ):
        self.server_url = server_url
        self.sample_rate = sample_rate
        self.metadata = metadata or {}
        self.interim_results = interim_results
        self.model = model
        self.api_key = api_key
        self.callback = callback
        self.last_send_time = time.time()

        # Transcript tracking
        self.current_transcript = []
        self.audio_stream_anchor_time = None
        self.last_word_received_time = None
        self.current_utterance_first_word_start_time = None
        self.current_utterance_last_word_stop_time = None

        # Semantic VAD
        self.semantic_vad_detected_pause = False
        self.speech_started = False

        # WebSocket state
        self.ws = None
        self.connected = False
        self.should_stop = False
        self.finished = False

        # Audio chunk queue for batching (thread-safe)
        self.audio_queue = deque(maxlen=100)  # Limit queue size
        self.queue_lock = asyncio.Lock() if hasattr(asyncio, "Lock") else None

        # Event loop and tasks
        self.loop = None
        self.connection_task = None
        self.sender_task = None
        self.receiver_task = None

        # Thread pool executor for running sync callbacks from async context
        self.executor = ThreadPoolExecutor(max_workers=2)

        # Start async loop in background thread
        self._start_async_loop()

    def _start_async_loop(self):
        """Start asyncio event loop in a separate thread."""
        import threading

        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            # Create lock after loop is set
            self.queue_lock = asyncio.Lock()

            try:
                # Start connection with retry
                self.connection_task = self.loop.create_task(self._connect_with_retry())
                self.loop.run_forever()
            except Exception as e:
                logger.error(f"Event loop error: {e}", exc_info=True)
            finally:
                # Clean up tasks
                if self.connection_task:
                    self.connection_task.cancel()
                self.loop.close()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()

        # Wait for connection (longer timeout for retries)
        start_time = time.time()
        while not self.connected and time.time() - start_time < 30:
            time.sleep(0.1)

        if not self.connected:
            logger.error("Failed to connect to Kyutai server after retries")

    async def _connect_with_retry(self):
        """Connect with exponential backoff retry."""
        max_retries = 5
        base_delay = 1.0

        for attempt in range(max_retries):
            if self.should_stop:
                break

            try:
                logger.info(f"Kyutai: Connection attempt {attempt + 1}/{max_retries}")
                await self._connect_and_run()
                # If we get here, connection was successful and closed normally
                if not self.should_stop:
                    logger.warning("Connection closed, will retry...")
                else:
                    break

            except asyncio.TimeoutError:
                logger.warning(f"Kyutai: Connection timeout (attempt {attempt + 1})")
            except websockets.exceptions.WebSocketException as e:
                logger.warning(f"Kyutai: WebSocket error (attempt {attempt + 1}): {e}")
            except Exception as e:
                logger.error(
                    f"Kyutai: Connection error (attempt {attempt + 1}): {e}",
                    exc_info=True,
                )

            # Don't retry if stopping
            if self.should_stop:
                break

            # Don't delay on last attempt
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt)  # Exponential backoff
                logger.info(f"Kyutai: Retrying in {delay:.1f}s...")
                await asyncio.sleep(delay)

        if not self.connected and not self.should_stop:
            logger.error("Kyutai: Failed to connect after all retries")

    async def _connect_and_run(self):
        """Connect to Kyutai server and start sender/receiver tasks."""
        try:
            # Build headers
            additional_headers = {}
            if self.api_key:
                additional_headers["kyutai-api-key"] = self.api_key

            # Connect with async websockets
            connect_kwargs = {
                "ping_interval": 20,
                "ping_timeout": 10,
                "open_timeout": 10,  # Add connection timeout
            }
            if additional_headers:
                connect_kwargs["additional_headers"] = additional_headers

            async with websockets.connect(self.server_url, **connect_kwargs) as websocket:
                self.ws = websocket
                self.connected = True
                logger.info("🔌 Kyutai WebSocket connection opened (async)")

                # Start sender and receiver tasks
                sender = asyncio.create_task(self._sender_loop())
                receiver = asyncio.create_task(self._receiver_loop())

                # Wait for both tasks (or until one fails)
                await asyncio.gather(sender, receiver)

        except websockets.exceptions.WebSocketException as e:
            logger.warning(f"WebSocket error: {e}")
            self.connected = False
            raise  # Re-raise for retry logic
        except asyncio.TimeoutError as e:
            logger.warning(f"Connection timeout: {e}")
            self.connected = False
            raise  # Re-raise for retry logic
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            self.connected = False
            raise  # Re-raise for retry logic
        finally:
            self.connected = False
            logger.info("🔌 Kyutai WebSocket connection closed")

    async def _sender_loop(self):
        """Background task to batch and send audio chunks."""
        logger.info("Kyutai: Sender loop started")
        batch = []
        last_send = time.time()

        try:
            while not self.should_stop:
                # Try to collect chunks for a batch
                try:
                    # Non-blocking check for chunks
                    while len(batch) < BATCH_SIZE:
                        async with self.queue_lock:
                            if self.audio_queue:
                                chunk = self.audio_queue.popleft()
                                batch.append(chunk)
                            else:
                                break

                    # Send batch if full or timeout reached
                    time_since_last_send = time.time() - last_send
                    should_send = len(batch) >= BATCH_SIZE or (batch and time_since_last_send >= BATCH_TIMEOUT)

                    if should_send and batch:
                        # Send all chunks in batch
                        for audio_data in batch:
                            message = self._prepare_audio_message(audio_data)
                            await self.ws.send(message)

                        self.last_send_time = time.time()
                        last_send = time.time()

                        # Log occasionally
                        if int(time.time() * 10) % 50 == 0:
                            logger.info(f"Kyutai: Sent batch of {len(batch)} chunks")

                        batch = []  # Clear batch

                    # Small sleep to prevent busy waiting
                    await asyncio.sleep(0.01)

                except Exception as e:
                    logger.error(f"Error in sender loop: {e}", exc_info=True)
                    await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            logger.info("Kyutai: Sender loop cancelled")
        except Exception as e:
            logger.error(f"Sender loop error: {e}", exc_info=True)
        finally:
            # Send any remaining chunks
            if batch:
                try:
                    for audio_data in batch:
                        message = self._prepare_audio_message(audio_data)
                        await self.ws.send(message)
                except Exception as e:
                    logger.error(f"Error sending final batch: {e}")

    def _prepare_audio_message(self, audio_data):
        """Prepare audio data for sending (resampling + serialization)."""
        # Resample if needed
        if self.sample_rate != KYUTAI_SAMPLE_RATE:
            audio_data, _ = audioop.ratecv(
                audio_data,
                SAMPLE_WIDTH,
                CHANNELS,
                self.sample_rate,
                KYUTAI_SAMPLE_RATE,
                None,
            )

        # Convert to float32
        audio_samples = np.frombuffer(audio_data, dtype=np.int16)
        audio_float = audio_samples.astype(np.float32) / 32768.0
        pcm_list = audio_float.tolist()

        # Pack with MessagePack
        return msgpack.packb(
            {"type": "Audio", "pcm": pcm_list},
            use_bin_type=True,
            use_single_float=True,
        )

    async def _receiver_loop(self):
        """Background task to receive and process messages."""
        logger.info("Kyutai: Receiver loop started")
        try:
            async for message in self.ws:
                if self.should_stop:
                    break
                await self._handle_message(message)
        except asyncio.CancelledError:
            logger.info("Kyutai: Receiver loop cancelled")
        except Exception as e:
            logger.error(f"Receiver loop error: {e}", exc_info=True)

    async def _handle_message(self, message):
        """Handle incoming message from Kyutai server."""
        try:
            data = msgpack.unpackb(message, raw=False)
            msg_type = data.get("type")

            if msg_type == "Word":
                self._handle_word_message(data)
            elif msg_type == "EndWord":
                self._handle_endword_message(data)
            elif msg_type == "Step":
                self._handle_step_message(data)
            elif msg_type == "Marker":
                self._handle_marker_message()
            elif msg_type == "Ready":
                self._handle_ready_message()
            else:
                logger.warning(f"Unknown message type: {msg_type}")

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)

    def _handle_word_message(self, data):
        """Handle Word message."""
        text = data.get("text", "")
        start_time = data.get("start_time", 0.0)

        if text:
            # Check for silence gap
            if self.current_transcript and self.current_utterance_last_word_stop_time is not None and start_time - self.current_utterance_last_word_stop_time > 1.0:
                self._emit_current_utterance()

            # Track first word
            if not self.current_transcript:
                self.current_utterance_first_word_start_time = start_time

            # Track timing
            self.last_word_received_time = time.time()
            self.speech_started = True

            # Add to transcript
            self.current_transcript.append({"text": text, "timestamp": [start_time, start_time]})

    def _handle_endword_message(self, data):
        """Handle EndWord message."""
        stop_time = data.get("stop_time", 0.0)
        if self.current_transcript:
            self.current_transcript[-1]["timestamp"][1] = stop_time
            self.current_utterance_last_word_stop_time = stop_time

    def _handle_step_message(self, data):
        """Handle Step message with semantic VAD."""
        if "prs" in data and len(data["prs"]) > PAUSE_PREDICTION_HEAD_INDEX:
            pause_prediction = data["prs"][PAUSE_PREDICTION_HEAD_INDEX]

            if pause_prediction > PAUSE_THRESHOLD and self.speech_started:
                self.semantic_vad_detected_pause = True
                self._check_and_emit_utterance()

        # Also check time-based silence
        self._check_and_emit_utterance()

    def _handle_marker_message(self):
        """Handle Marker message."""
        logger.info("Kyutai: End of stream marker received")
        self._emit_current_utterance()

    def _handle_ready_message(self):
        """Handle Ready message."""
        self.audio_stream_anchor_time = time.time()
        logger.info("🎯 Kyutai: Audio stream anchor set (Ready signal)")

    def send(self, audio_data):
        """
        Send audio data (non-blocking).

        Adds audio to queue for batched sending.

        Args:
            audio_data: Audio data as bytes (int16 PCM)
        """
        if not self.connected or self.should_stop or self.finished:
            return

        try:
            # Add to queue (will be batched by sender task)
            if self.loop and self.loop.is_running():
                # Schedule the append on the event loop
                asyncio.run_coroutine_threadsafe(self._add_to_queue(audio_data), self.loop)
            else:
                # Fallback: direct append with lock handling
                self.audio_queue.append(audio_data)
        except Exception as e:
            logger.error(f"Error queuing audio: {e}", exc_info=True)

    async def _add_to_queue(self, audio_data):
        """Add audio to queue (async)."""
        async with self.queue_lock:
            self.audio_queue.append(audio_data)

    def _check_and_emit_utterance(self):
        """Check if utterance should be emitted."""
        if not self.current_transcript:
            return

        if self.last_word_received_time is None:
            return

        # Priority 1: Semantic VAD
        if self.semantic_vad_detected_pause:
            self._emit_current_utterance()
            self.semantic_vad_detected_pause = False
            return

        # Priority 2: Time-based silence
        current_time = time.time()
        silence_duration = current_time - self.last_word_received_time

        if len(self.current_transcript) == 1:
            if self.current_utterance_last_word_stop_time is None:
                if silence_duration > 1.0:
                    self._emit_current_utterance()
            else:
                if silence_duration > 0.25:
                    self._emit_current_utterance()
        else:
            if silence_duration > 0.5:
                self._emit_current_utterance()

    def _emit_current_utterance(self):
        """Emit current transcript as utterance."""
        if self.current_transcript and self.callback:
            transcript_text = " ".join([w["text"] for w in self.current_transcript])

            # Calculate timing
            if self.audio_stream_anchor_time is not None and self.current_utterance_first_word_start_time is not None:
                timestamp_ms = int((self.audio_stream_anchor_time + self.current_utterance_first_word_start_time) * 1000)

                if self.current_utterance_last_word_stop_time is not None:
                    duration_seconds = self.current_utterance_last_word_stop_time - self.current_utterance_first_word_start_time
                    duration_ms = int(duration_seconds * 1000)
                else:
                    if self.current_transcript:
                        last_word_start = self.current_transcript[-1]["timestamp"][0]
                        duration_seconds = last_word_start - self.current_utterance_first_word_start_time
                        duration_ms = int(duration_seconds * 1000)
                    else:
                        duration_ms = 0
            else:
                timestamp_ms = int(time.time() * 1000)
                duration_ms = 0

            # Call callback in thread pool to avoid Django ORM async context issues
            metadata = {
                "duration_ms": duration_ms,
                "timestamp_ms": timestamp_ms,
            }
            if self.loop and self.executor:
                # Run sync callback in thread pool from async context
                self.loop.run_in_executor(self.executor, self.callback, transcript_text, metadata)
            else:
                # Fallback for edge cases (shouldn't happen)
                self.callback(transcript_text, metadata)

            # Reset state
            self.current_transcript = []
            self.current_utterance_first_word_start_time = None
            self.current_utterance_last_word_stop_time = None
            self.last_word_received_time = None
            self.semantic_vad_detected_pause = False
            self.speech_started = False

    def finish(self):
        """Close connection and clean up."""
        if self.finished:
            return

        self.finished = True
        logger.info("Finishing Kyutai streaming transcriber")

        # Emit remaining transcript
        self._emit_current_utterance()
        self.should_stop = True

        # Schedule cleanup on event loop
        if self.loop and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._finish_async(), self.loop)
            try:
                future.result(timeout=2.0)
            except Exception as e:
                logger.error(f"Error during finish: {e}")

        # Stop event loop
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

        # Shutdown executor
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=False)

    async def _finish_async(self):
        """Async cleanup."""
        try:
            if self.ws and not self.ws.closed:
                # Send marker
                marker_msg = msgpack.packb({"type": "Marker", "id": 0}, use_bin_type=True)
                await self.ws.send(marker_msg)
                await asyncio.sleep(0.5)
                await self.ws.close()
        except Exception as e:
            logger.error(f"Error in async finish: {e}")
