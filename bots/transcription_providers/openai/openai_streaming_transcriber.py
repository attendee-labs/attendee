import asyncio
import audioop
import base64
import logging
import os
import queue
import threading
import time

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

OPENAI_REALTIME_SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 1  # mono
OPENAI_APPEND_FRAME_BYTES = int(OPENAI_REALTIME_SAMPLE_RATE * SAMPLE_WIDTH * 0.1)  # 100ms


class OpenAIStreamingTranscriber:
    def __init__(
        self,
        *,
        openai_api_key,
        connection_model,
        transcription_model,
        sample_rate,
        metadata=None,
        language=None,
        prompt=None,
        save_utterance_callback=None,
        max_retry_time=120,
    ):
        self.openai_api_key = openai_api_key
        self.connection_model = connection_model
        self.transcription_model = transcription_model
        self.sample_rate = sample_rate
        self.metadata = metadata or {}
        self.language = language
        self.prompt = prompt
        self.save_utterance_callback = save_utterance_callback
        self.max_retry_time = max_retry_time

        self._participant_name = self.metadata.get("participant_full_name", "Unknown")
        self._resampler_state = None
        self._audio_buffer = bytearray()

        self.last_send_time = time.time()

        self._loop = None
        self._loop_thread = None
        self._send_queue = None
        self._connection = None
        self._client = None
        self._sender_task = None
        self._receiver_task = None
        self._active_connection_model = None
        self._session_update_fallback_attempted = False
        self._callback_queue = queue.Queue()
        self._callback_worker_stop = threading.Event()
        self._callback_worker_thread = threading.Thread(
            target=self._callback_worker_loop,
            daemon=True,
            name="openai-callback-worker",
        )
        self._callback_worker_thread.start()

        self.connected = False
        self.reconnecting = True
        self.should_stop = False

        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
        self._start_event_loop()

    def _callback_worker_loop(self):
        while not self._callback_worker_stop.is_set():
            try:
                callback_data = self._callback_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if callback_data is None:
                self._callback_queue.task_done()
                break

            transcript_text, metadata = callback_data
            try:
                self.save_utterance_callback(transcript_text, metadata)
            except Exception as e:
                logger.error(f"[{self._participant_name}] Error in OpenAI save_utterance_callback: {e}", exc_info=True)
            finally:
                self._callback_queue.task_done()

    def _connection_model_candidates(self):
        candidates = [
            self.connection_model,
            self.transcription_model,
            "gpt-realtime",
            "gpt-4o-realtime-preview",
            "gpt-4o-transcribe-latest",
            "gpt-4o-transcribe",
            "gpt-4o-mini-transcribe",
        ]
        deduped = []
        for candidate in candidates:
            if candidate and candidate not in deduped:
                deduped.append(candidate)
        return deduped

    def _start_event_loop(self):
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_event_loop, daemon=True, name="openai-event-loop")
        self._loop_thread.start()

        time.sleep(0.1)
        asyncio.run_coroutine_threadsafe(self._connect(), self._loop)

    def _run_event_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _connect(self):
        attempt = 0
        start_time = time.time()
        backoff_seconds = [1, 2, 4, 8]

        self._client = AsyncOpenAI(api_key=self.openai_api_key, base_url=self.base_url)

        try:
            while not self.should_stop:
                elapsed = time.time() - start_time
                if elapsed >= self.max_retry_time:
                    logger.error(f"[{self._participant_name}] OpenAI realtime connection timed out after {self.max_retry_time}s")
                    self.reconnecting = False
                    return

                attempt += 1
                try:
                    logger.info(f"[{self._participant_name}] Connecting to OpenAI realtime via SDK (attempt {attempt})")

                    connected = False
                    for connection_model in self._connection_model_candidates():
                        try:
                            async with self._client.realtime.connect(model=connection_model) as connection:
                                self._connection = connection
                                self._active_connection_model = connection_model
                                self.connected = True
                                self.reconnecting = False
                                self._send_queue = asyncio.Queue()

                                await self._send_session_update()

                                self._receiver_task = asyncio.create_task(self._receiver_loop())
                                self._sender_task = asyncio.create_task(self._sender_loop())

                                await asyncio.gather(self._receiver_task, self._sender_task, return_exceptions=True)

                            connected = True
                            break
                        except Exception as connection_error:
                            if "invalid_model" in str(connection_error).lower():
                                logger.warning(f"[{self._participant_name}] Realtime model '{connection_model}' was rejected, trying next fallback")
                                continue
                            raise

                    if not connected and not self.should_stop:
                        raise Exception("No valid OpenAI realtime connection model available")

                    if self.should_stop:
                        return
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.warning(f"[{self._participant_name}] OpenAI realtime SDK connection error: {e}")

                self.connected = False
                self.reconnecting = True
                self._connection = None
                self._send_queue = None

                delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                await asyncio.sleep(delay)
        finally:
            self.reconnecting = False
            self.connected = False
            if self._client:
                await self._client.close()

    def _build_transcription_session_update_event(self):
        transcription_config = {"model": self.transcription_model}
        if self.language:
            transcription_config["language"] = self.language
        if self.prompt:
            transcription_config["prompt"] = self.prompt

        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": OPENAI_REALTIME_SAMPLE_RATE,
                        },
                        "transcription": transcription_config,
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                        },
                    },
                },
                "include": ["item.input_audio_transcription.logprobs"],
            },
        }

    def _build_realtime_session_update_event(self):
        transcription_config = {"model": self.transcription_model}
        if self.language:
            transcription_config["language"] = self.language
        if self.prompt:
            transcription_config["prompt"] = self.prompt

        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": OPENAI_REALTIME_SAMPLE_RATE,
                        },
                        "transcription": transcription_config,
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                        },
                    }
                },
                "include": ["item.input_audio_transcription.logprobs"],
            },
        }

    def _build_legacy_realtime_session_update_event(self):
        transcription_config = {"model": self.transcription_model}
        if self.language:
            transcription_config["language"] = self.language
        if self.prompt:
            transcription_config["prompt"] = self.prompt

        return {
            "type": "session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": transcription_config,
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                },
            },
        }

    async def _send_session_update(self):
        if not self._connection:
            return

        model = (self._active_connection_model or "").lower()
        if "transcribe" in model:
            event = self._build_transcription_session_update_event()
        else:
            event = self._build_realtime_session_update_event()

        await self._connection.send(event)

    async def _send_session_update_realtime_fallback(self):
        if not self._connection:
            return

        try:
            await self._connection.send(self._build_realtime_session_update_event())
        except Exception:
            await self._connection.send(self._build_legacy_realtime_session_update_event())

    async def _sender_loop(self):
        while not self.should_stop:
            try:
                payload = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if not self.connected or not self._connection:
                    return
                continue

            if not self.connected or not self._connection:
                return

            try:
                await self._connection.send(payload)
            except Exception as e:
                logger.warning(f"[{self._participant_name}] OpenAI realtime SDK send failed: {e}")
                self.connected = False
                return

    def _event_to_dict(self, event):
        if isinstance(event, dict):
            return event
        if hasattr(event, "model_dump"):
            return event.model_dump()
        if hasattr(event, "to_dict"):
            return event.to_dict()
        return {}

    async def _receiver_loop(self):
        try:
            async for event in self._connection:
                message = self._event_to_dict(event)
                await self._handle_realtime_message(message)
        except Exception as e:
            if not self.should_stop:
                logger.warning(f"[{self._participant_name}] OpenAI realtime SDK receiver closed unexpectedly: {e}")
        finally:
            self.connected = False

    def _extract_transcript_text(self, message):
        transcript = message.get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            return transcript.strip()

        item = message.get("item")
        if not isinstance(item, dict):
            return None

        content = item.get("content")
        if not isinstance(content, list):
            return None

        text_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("transcript") or part.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())

        if text_parts:
            return " ".join(text_parts)
        return None

    async def _handle_realtime_message(self, message):
        message_type = message.get("type")

        if message_type == "conversation.item.input_audio_transcription.completed":
            transcript_text = self._extract_transcript_text(message)
            if transcript_text:
                self._emit_utterance(transcript_text)
            return

        if message_type == "conversation.item.input_audio_transcription.failed":
            logger.warning(f"[{self._participant_name}] OpenAI realtime transcription failed: {message}")
            return

        if message_type == "error":
            logger.error(f"[{self._participant_name}] OpenAI realtime error event: {message}")
            error_message = (message.get("error") or {}).get("message", "")
            if (
                "transcription session update event" in str(error_message).lower()
                and not self._session_update_fallback_attempted
            ):
                self._session_update_fallback_attempted = True
                logger.info(f"[{self._participant_name}] Retrying session.update with realtime-compatible payload")
                try:
                    await self._send_session_update_realtime_fallback()
                except Exception as fallback_error:
                    logger.error(f"[{self._participant_name}] Realtime fallback session.update failed: {fallback_error}")

    def _emit_utterance(self, transcript_text):
        if not self.save_utterance_callback:
            return

        metadata = {
            "timestamp_ms": int(time.time() * 1000),
            "duration_ms": 0,
        }

        try:
            self._callback_queue.put((transcript_text, metadata))
        except Exception as e:
            logger.error(f"[{self._participant_name}] Error queueing utterance callback: {e}", exc_info=True)

    def _enqueue_audio_append(self, chunk):
        if not self._loop or not self._send_queue:
            return

        payload = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(chunk).decode("ascii"),
        }

        try:
            self._loop.call_soon_threadsafe(self._send_queue.put_nowait, payload)
        except Exception as e:
            logger.warning(f"[{self._participant_name}] Failed to enqueue OpenAI audio chunk: {e}")
            self.connected = False

    def send(self, audio_data):
        if not self.connected and not self.reconnecting and not self.should_stop:
            raise ConnectionError("OpenAI realtime SDK connection failed permanently")

        if not self.connected or self.should_stop:
            return

        self.last_send_time = time.time()

        try:
            if self.sample_rate != OPENAI_REALTIME_SAMPLE_RATE:
                audio_data, self._resampler_state = audioop.ratecv(
                    audio_data,
                    SAMPLE_WIDTH,
                    CHANNELS,
                    self.sample_rate,
                    OPENAI_REALTIME_SAMPLE_RATE,
                    self._resampler_state,
                )

            self._audio_buffer.extend(audio_data)

            while len(self._audio_buffer) >= OPENAI_APPEND_FRAME_BYTES:
                chunk = bytes(self._audio_buffer[:OPENAI_APPEND_FRAME_BYTES])
                del self._audio_buffer[:OPENAI_APPEND_FRAME_BYTES]
                self._enqueue_audio_append(chunk)
        except Exception as e:
            logger.error(f"[{self._participant_name}] Error while queuing OpenAI audio: {e}", exc_info=True)
            self.connected = False

    async def _flush_buffer_and_commit(self):
        if self._send_queue and self._connection and self.connected:
            while True:
                try:
                    pending_payload = self._send_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await self._connection.send(pending_payload)

        if self._audio_buffer and self._connection and self.connected:
            await self._connection.send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(bytes(self._audio_buffer)).decode("ascii"),
                }
            )
            self._audio_buffer = bytearray()

        if self._connection and self.connected:
            await self._connection.send({"type": "input_audio_buffer.commit"})
            await asyncio.sleep(0.2)

    def finish(self):
        if self.should_stop:
            return

        self.should_stop = True
        logger.info(f"Finishing OpenAI transcriber [{self._participant_name}]")

        try:
            if self._loop and self._loop.is_running():

                async def flush_and_close():
                    try:
                        await self._flush_buffer_and_commit()
                        if self._connection and hasattr(self._connection, "close"):
                            await self._connection.close()
                        if self._client:
                            await self._client.close()
                    except Exception as e:
                        logger.warning(f"[{self._participant_name}] Error closing OpenAI realtime SDK connection: {e}")

                future = asyncio.run_coroutine_threadsafe(flush_and_close(), self._loop)
                try:
                    future.result(timeout=2)
                except Exception:
                    pass

                self._loop.call_soon_threadsafe(self._loop.stop)

            if self._loop_thread and self._loop_thread.is_alive():
                self._loop_thread.join(timeout=2)
        finally:
            self._callback_worker_stop.set()
            try:
                self._callback_queue.put(None)
            except Exception:
                pass
            if self._callback_worker_thread and self._callback_worker_thread.is_alive():
                self._callback_worker_thread.join(timeout=2)

            self.connected = False
            self.reconnecting = False
