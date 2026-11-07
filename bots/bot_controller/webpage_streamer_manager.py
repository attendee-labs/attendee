import logging
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)

OUTPUT_DESTINATIONS = ("webcam", "screenshare")


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
        # The webpage streamer runs a separate browser per output destination, so each destination has its own url and WebRTC connection
        self.urls = {output_destination: None for output_destination in OUTPUT_DESTINATIONS}
        self.webrtc_connection_started = {output_destination: False for output_destination in OUTPUT_DESTINATIONS}
        self.get_peer_connection_offer_callback = get_peer_connection_offer_callback
        self.start_peer_connection_callback = start_peer_connection_callback
        self.cleaned_up = False
        self.webpage_streamer_service_hostname = webpage_streamer_service_hostname
        self.is_bot_ready_for_webpage_streamer_callback = is_bot_ready_for_webpage_streamer_callback
        self.play_bot_output_media_stream_callback = play_bot_output_media_stream_callback
        self.stop_bot_output_media_stream_callback = stop_bot_output_media_stream_callback
        self.on_message_that_webpage_streamer_connection_can_start_callback = on_message_that_webpage_streamer_connection_can_start_callback
        self.keepalive_task = None
        self.webpage_streamer_connection_can_start = False

    def init(self):
        if self.keepalive_task is not None:
            return

        self.keepalive_task = threading.Thread(target=self.send_webpage_streamer_keepalive_periodically, daemon=True)
        self.keepalive_task.start()

    def update(self, urls_by_output_destination):
        if not self.webpage_streamer_connection_can_start:
            logger.info("In WebpageStreamerManager.update, Webpage streamer connection can not start yet. Not updating.")
            return

        for output_destination in OUTPUT_DESTINATIONS:
            self.update_output_destination(output_destination, urls_by_output_destination.get(output_destination))

    # Possible cases:
    # 1. Url went from empty to non-empty: load the page and start rendering it in the output destination.
    # 2. Url changed from one non-empty value to another: load the new page. It is already being rendered.
    # 3. Url went from non-empty to empty: stop rendering it in the output destination.
    def update_output_destination(self, output_destination, url):
        previous_url = self.urls[output_destination]
        if (url or None) == (previous_url or None):
            return

        if url:
            self.start_or_update_webrtc_connection(output_destination, url)
            if not previous_url:
                logger.info(f"Playing bot output media stream to {output_destination}")
                self.play_bot_output_media_stream_callback(output_destination)
        else:
            logger.info(f"Stopping bot output media stream to {output_destination} because url is empty")
            self.stop_bot_output_media_stream_callback(output_destination)

        self.urls[output_destination] = url

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

    def update_webrtc_connection(self, output_destination, url):
        # Start and update do the same thing, so we can use the same endpoint
        update_streaming_response = requests.post(f"http://{self.streaming_service_hostname()}:8000/start_streaming", json={"url": url, "output_destination": output_destination})
        logger.info(f"Update streaming response for {output_destination}: {update_streaming_response}")

        if update_streaming_response.status_code != 200:
            logger.info(f"Failed to update streaming for {output_destination}. Response: {update_streaming_response.status_code}")
            return

    def start_or_update_webrtc_connection(self, output_destination, url):
        if self.webrtc_connection_started[output_destination]:
            return self.update_webrtc_connection(output_destination, url)

        logger.info(f"Open webpage streaming connection for {output_destination}. url={url}")
        peerConnectionOffer = self.get_peer_connection_offer_callback(output_destination)
        logger.info(f"Peer connection offer for {output_destination}: {peerConnectionOffer}")
        if peerConnectionOffer.get("error"):
            logger.error(f"Error getting peer connection offer for {output_destination}: {peerConnectionOffer.get('error')}, returning")
            return

        offer_response = requests.post(
            f"http://{self.streaming_service_hostname()}:8000/offer",
            json={"sdp": peerConnectionOffer["sdp"], "type": peerConnectionOffer["type"], "output_destination": output_destination},
        )
        logger.info(f"Offer response for {output_destination}: {offer_response.json()}")
        self.start_peer_connection_callback(offer_response.json(), output_destination)

        start_streaming_response = requests.post(f"http://{self.streaming_service_hostname()}:8000/start_streaming", json={"url": url, "output_destination": output_destination})
        logger.info(f"Start streaming response for {output_destination}: {start_streaming_response}")

        if start_streaming_response.status_code != 200:
            logger.info(f"Failed to start streaming for {output_destination}. Response: {start_streaming_response.status_code}")
            return

        self.webrtc_connection_started[output_destination] = True

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
