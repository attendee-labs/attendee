import os
import time
from unittest.mock import MagicMock

import numpy as np


class MockVideoFrame:
    def __init__(self):
        width = 640
        height = 360

        # Create separate Y, U, and V planes
        self.y_buffer = b"\x00" * (width * height)  # Y plane (black)
        self.u_buffer = b"\x80" * (width * height // 4)  # U plane (128 for black)
        self.v_buffer = b"\x80" * (width * height // 4)  # V plane (128 for black)

        self.size = len(self.y_buffer) + len(self.u_buffer) + len(self.v_buffer)
        self.timestamp = int(time.time() * 1000)  # Current time in milliseconds

    def GetBuffer(self):
        return self.y_buffer + self.u_buffer + self.v_buffer

    def GetYBuffer(self):
        return self.y_buffer

    def GetUBuffer(self):
        return self.u_buffer

    def GetVBuffer(self):
        return self.v_buffer

    def GetStreamWidth(self):
        return 640

    def GetStreamHeight(self):
        return 360


class MockPCMAudioFrame:
    def __init__(self):
        # Create 10ms of a 440Hz sine wave at 32000Hz mono
        # 32000 samples/sec * 0.01 sec = 320 samples
        # Each sample is 2 bytes (unsigned 16-bit)
        import math

        samples = []
        for i in range(320):  # 10ms worth of samples at 32kHz
            # Generate sine wave with frequency 440Hz
            t = i / 32000.0  # time in seconds
            # Generate value between 0 and 65535 (unsigned 16-bit)
            # Center at 32768, use amplitude of 16384 to avoid clipping
            value = int(32768 + 16384 * math.sin(2 * math.pi * 440 * t))
            # Ensure value stays within valid range
            value = max(0, min(65535, value))
            # Convert to two bytes (little-endian)
            samples.extend([value & 0xFF, (value >> 8) & 0xFF])
        self.buffer = bytes(samples)

    def GetBuffer(self):
        return self.buffer

    def GetSampleRate(self):
        return 32000

    def GetChannelNum(self):
        return 1


class MockF32AudioFrame:
    def __init__(self):
        # Create 10ms of a 440Hz sine wave at 48000Hz mono
        # 48000 samples/sec * 0.01 sec = 480 samples
        # Each sample is a 32-bit float between -1.0 and 1.0
        import math
        import struct

        samples = []
        for i in range(480):  # 10ms worth of samples at 48kHz
            # Generate sine wave with frequency 440Hz
            t = i / 48000.0  # time in seconds
            # Generate value between -1.0 and 1.0
            value = 0.8 * math.sin(2 * math.pi * 440 * t)  # Using 0.8 amplitude to avoid clipping
            # Pack float to bytes (4 bytes per sample)
            samples.extend(struct.pack("f", value))
        self.buffer = bytes(samples)

    def GetBuffer(self):
        return self.buffer


# Simulate video data arrival
# Create a mock video message in the format expected by process_video_frame
def create_mock_video_frame(width=640, height=480):
    # Create a bytearray for the message
    mock_video_message = bytearray()

    # Add message type (2 for VIDEO) as first 4 bytes
    mock_video_message.extend((2).to_bytes(4, byteorder="little"))

    # Add timestamp (12345) as next 8 bytes
    mock_video_message.extend((12345).to_bytes(8, byteorder="little"))

    # Add stream ID length (4) and stream ID ("main") - total 8 bytes
    stream_id = "main"
    mock_video_message.extend(len(stream_id).to_bytes(4, byteorder="little"))
    mock_video_message.extend(stream_id.encode("utf-8"))

    # Add width and height - 8 bytes
    mock_video_message.extend(width.to_bytes(4, byteorder="little"))
    mock_video_message.extend(height.to_bytes(4, byteorder="little"))

    # Create I420 frame data (Y, U, V planes)
    # Y plane: width * height bytes
    y_plane_size = width * height
    y_plane = np.ones(y_plane_size, dtype=np.uint8) * 128  # mid-gray

    # U and V planes: (width//2 * height//2) bytes each
    uv_width = (width + 1) // 2  # half_ceil implementation
    uv_height = (height + 1) // 2
    uv_plane_size = uv_width * uv_height

    u_plane = np.ones(uv_plane_size, dtype=np.uint8) * 128  # no color tint
    v_plane = np.ones(uv_plane_size, dtype=np.uint8) * 128  # no color tint

    # Add the frame data to the message
    mock_video_message.extend(y_plane.tobytes())
    mock_video_message.extend(u_plane.tobytes())
    mock_video_message.extend(v_plane.tobytes())

    return mock_video_message


def create_mock_file_uploader():
    mock_file_uploader = MagicMock()
    mock_file_uploader.upload_file.return_value = None
    mock_file_uploader.wait_for_upload.return_value = None
    mock_file_uploader.delete_file.return_value = None
    mock_file_uploader.filename = "test-recording-key"
    return mock_file_uploader


def create_mock_google_meet_driver():
    mock_driver = MagicMock()
    mock_driver.execute_script.side_effect = [
        None,  # First call (window.ws.enableMediaSending())
        12345,  # Second call (performance.timeOrigin)
    ]

    # Make save_screenshot actually create an empty PNG file
    def mock_save_screenshot(filepath):
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        # Create empty file
        with open(filepath, "wb") as f:
            # Write minimal valid PNG file bytes
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
        return filepath

    mock_driver.save_screenshot.side_effect = mock_save_screenshot
    return mock_driver


def create_mock_display():
    mock_display = MagicMock()
    mock_display.new_display_var = ":99"
    return mock_display
