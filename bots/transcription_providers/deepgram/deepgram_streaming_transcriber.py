import logging
import time

from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveOptions,
    LiveTranscriptionEvents,
)

logger = logging.getLogger(__name__)


class DeepgramStreamingTranscriber:
    def __init__(self, *, deepgram_api_key, interim_results, language, model, sample_rate, metadata, callback, redaction_settings=None):
        # Configure the DeepgramClientOptions to enable KeepAlive for maintaining the WebSocket connection (only if necessary to your scenario)
        config = DeepgramClientOptions(options={"keepalive": "true"})

        self.last_send_time = time.time()

        # Create a websocket connection using the DEEPGRAM_API_KEY from environment variables
        self.deepgram = DeepgramClient(deepgram_api_key, config)

        # Use the listen.live class to create the websocket connection
        self.dg_connection = self.deepgram.listen.websocket.v("1")

        def on_message(self, result, **kwargs):
            sentence = result.channel.alternatives[0].transcript
            if len(sentence) == 0:
                return
            logger.info(f"Transcription: {sentence}")

        self.dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)

        def on_error(self, error, **kwargs):
            logger.error(f"Error in Deepgram streaming transcription: {error}")
            # Check if error is related to redaction configuration
            error_str = str(error).lower()
            if redaction_settings and ("redact" in error_str or "invalid" in error_str):
                logger.warning(f"Redaction configuration may have caused error with settings {redaction_settings}, but transcription will continue")

        self.dg_connection.on(LiveTranscriptionEvents.Error, on_error)

        # Log redaction settings being applied
        if redaction_settings:
            logger.info(f"Applying Deepgram streaming redaction settings: {redaction_settings}")
        else:
            logger.debug("No redaction settings configured for Deepgram streaming transcription")

        options = LiveOptions(
            model=model,
            smart_format=True,
            language=language,
            encoding="linear16",
            sample_rate=sample_rate,
            interim_results=interim_results,
            extra=metadata,
            callback=callback,
            redact=redaction_settings,
        )

        self.dg_connection.start(options)
        if redaction_settings:
            logger.info(f"Deepgram streaming connection started successfully with redaction: {redaction_settings}")

    def send(self, data):
        self.dg_connection.send(data)
        self.last_send_time = time.time()

    def finish(self):
        self.dg_connection.finish()
