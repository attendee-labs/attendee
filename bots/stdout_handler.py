"""
Custom logging handler that forces stdout instead of stderr.
"""

import logging
import sys


class StdoutStreamHandler(logging.StreamHandler):
    """
    A StreamHandler that always writes to stdout, never stderr.
    This ensures GCP Cloud Logging assigns correct severity levels.
    """

    def __init__(self, *args, **kwargs):
        # Force stream to be stdout, ignore any stream parameter
        kwargs["stream"] = sys.stdout
        super().__init__(*args, **kwargs)
