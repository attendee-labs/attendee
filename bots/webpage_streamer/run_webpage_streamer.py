#!/usr/bin/env python3
"""
Standalone script to run WebpageStreamer without Django.

This script allows you to run the webpage streaming functionality
independently of the Django framework.
"""

import logging
import os
import sys

# Add the project root to Python path so we can import from bots
# This file is in bots/webpage_streamer/, so go up 2 levels to reach project root
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.join(current_dir, "..", "..")
sys.path.insert(0, project_root)

from bots.webpage_streamer.webpage_streamer import WebpageStreamer


def setup_logging():
    """Configure basic logging for the standalone script."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", handlers=[logging.StreamHandler(sys.stdout)])


def main():
    """Main function to run the webpage streamer."""
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("Starting WebpageStreamer as standalone script...")

    try:
        webpage_streamer = WebpageStreamer()
        webpage_streamer.run()
    except KeyboardInterrupt:
        logger.info("Shutting down due to keyboard interrupt...")
    except Exception as e:
        logger.error(f"Error running WebpageStreamer: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
