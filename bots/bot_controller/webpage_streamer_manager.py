import logging
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)


class WebpageStreamerManager:
    def __init__(
        self,
        is_bot_ready_for_webpage_streamer_callback,
        get_peer_connection_offer_callback,
        start_peer_connection_callback,
        play_bot_output_media_stream_callback,
        stop_bot_output_media_stream_callback,
        on_message_that_webpage_streamer_connection_can_start_callback,
        webpage_streamer_service_hostname,
    ):
        self.url = None
        self.last_non_empty_url = None
        self.output_destination = None
        self.get_peer_connection_offer_callback = get_peer_connection_offer_callback
        self.start_peer_connection_callback = start_peer_connection_callback
        self.cleaned_up = False
        self.webpage_streamer_service_hostname = webpage_streamer_service_hostname
        self.is_bot_ready_for_webpage_streamer_callback = is_bot_ready_for_webpage_streamer_callback
        self.play_bot_output_media_stream_callback = play_bot_output_media_stream_callback
        self.stop_bot_output_media_stream_callback = stop_bot_output_media_stream_callback
        self.on_message_that_webpage_streamer_connection_can_start_callback = on_message_that_webpage_streamer_connection_can_start_callback
        self.webrtc_connection_started = False
        self.keepalive_task = None
        self.webpage_streamer_connection_can_start = False

    def init(self):
        if self.keepalive_task is not None:
            return

        self.keepalive_task = threading.Thread(target=self.send_webpage_streamer_keepalive_periodically, daemon=True)
        self.keepalive_task.start()

    # Possible cases:
    # 1. Streaming has not started yet.
    # 2. Streaming has started. URL has changed.
    # 3. Streaming has started. Output destination has changed.
    # 4. Streaming has started. URL and output destination have changed.
    def update(self, url, output_destination):
        if not self.webpage_streamer_connection_can_start:
            logger.info("In WebpageStreamerManager.update, Webpage streamer connection can not start yet. Not updating.")
            return

        sleep_before_playing_bot_output_media_stream = False
        if url != self.url or output_destination != self.output_destination:
            if url:
                if url != self.url:
                    self.start_or_update_webrtc_connection(url)
                    # If we are shifting to a new output destination AND the page is set to a different url, then let's pause for a second
                    # Otherwise it will display the old page for a bit
                    if output_destination != self.output_destination and self.last_non_empty_url and self.last_non_empty_url != url:
                        sleep_before_playing_bot_output_media_stream = True
                if output_destination != self.output_destination and self.output_destination:
                    logger.info("Stopping bot output media stream")
                    self.stop_bot_output_media_stream_callback()
                    sleep_before_playing_bot_output_media_stream = True  # Seems like there's sometimes a DOM glitch if we don't wait a bit. Not ideal.
                # Tell the adapter to start rendering the bot output media stream in the webcam / screenshare
                if sleep_before_playing_bot_output_media_stream:
                    time.sleep(1)
                only_change_was_url = url != self.url and output_destination == self.output_destination
                if not only_change_was_url:
                    logger.info(f"Playing bot output media stream to {output_destination}")
                    self.play_bot_output_media_stream_callback(output_destination)
            if not url:
                logger.info("Stopping bot output media stream because url is empty")
                self.stop_bot_output_media_stream_callback()

        self.url = url
        self.output_destination = output_destination
        if url:
            self.last_non_empty_url = url

    def cleanup(self):
        try:
            self.send_webpage_streamer_shutdown_request()
        except Exception as e:
            logger.warning(f"Error sending webpage streamer shutdown request: {e}")
        self.cleaned_up = True

    def streaming_service_hostname(self):
        # If we're running in k8s, the streaming service will be on another pod which is addressable using via a per-pod service
        if os.getenv("LAUNCH_BOT_METHOD") == "kubernetes":
            return f"{self.webpage_streamer_service_hostname}"
        # Otherwise the streaming service will be running in a separate docker compose service, so we address it using the service name
        return "attendee-webpage-streamer-local"

    def update_webrtc_connection(self, url):
        # Start and update do the same thing, so we can use the same endpoint
        update_streaming_response = requests.post(f"http://{self.streaming_service_hostname()}:8000/start_streaming", json={"url": url})
        logger.info(f"Update streaming response: {update_streaming_response}")

        if update_streaming_response.status_code != 200:
            logger.info(f"Failed to update streaming. Response: {update_streaming_response.status_code}")
            return

    def start_or_update_webrtc_connection(self, url):
        if self.webrtc_connection_started:
            return self.update_webrtc_connection(url)

        logger.info(f"Open webpage streaming connection. Settings are url={self.url} and output_destination={self.output_destination}")
        peerConnectionOffer = self.get_peer_connection_offer_callback()
        logger.info(f"Peer connection offer: {peerConnectionOffer}")
        if peerConnectionOffer.get("error"):
            logger.error(f"Error getting peer connection offer: {peerConnectionOffer.get('error')}, returning")
            return

        offer_response = requests.post(f"http://{self.streaming_service_hostname()}:8000/offer", json={"sdp": peerConnectionOffer["sdp"], "type": peerConnectionOffer["type"]})
        logger.info(f"Offer response: {offer_response.json()}")
        self.start_peer_connection_callback(offer_response.json())

        start_streaming_response = requests.post(f"http://{self.streaming_service_hostname()}:8000/start_streaming", json={"url": url})
        logger.info(f"Start streaming response: {start_streaming_response}")

        if start_streaming_response.status_code != 200:
            logger.info(f"Failed to start streaming, not starting webpage streamer keepalive task. Response: {start_streaming_response.status_code}")
            return

        self.webrtc_connection_started = True

    def send_webpage_streamer_keepalive_periodically(self):
        """Send keepalive requests to the streaming service periodically."""
        while not self.cleaned_up:
            try:
                if not self.webpage_streamer_connection_can_start:
                    time.sleep(1)
                else:
                    time.sleep(60)  # Wait 60 seconds between keepalive requests if we know it's started

                if self.cleaned_up:
                    break

                response = requests.post(f"http://{self.streaming_service_hostname()}:8000/keepalive", json={})
                logger.info(f"Webpage streamer keepalive response: {response.status_code}")
                if response.status_code == 200 and not self.webpage_streamer_connection_can_start:
                    bot_is_ready_for_webpage_streamer = self.is_bot_ready_for_webpage_streamer_callback()
                    if bot_is_ready_for_webpage_streamer:
                        logger.info("Webpage streamer has started and bot is ready for webpage streamer. Notifying bot controller.")
                        self.webpage_streamer_connection_can_start = True
                        self.on_message_that_webpage_streamer_connection_can_start_callback()
                    else:
                        logger.info("Webpage streamer has started but bot is not ready for webpage streamer. Not notifying bot controller.")

            except Exception as e:
                logger.info(f"Failed to send webpage streamer keepalive: {e}")
                # Continue the loop even if a single keepalive fails

        logger.info("Webpage streamer keepalive task stopped")

    def send_webpage_streamer_shutdown_request(self):
        try:
            response = requests.post(f"http://{self.streaming_service_hostname()}:8000/shutdown", json={})
            logger.info(f"Webpage streamer shutdown response: {response.json()}")
        except Exception as e:
            logger.info(f"Webpage streamer shutdown response: {e}")
