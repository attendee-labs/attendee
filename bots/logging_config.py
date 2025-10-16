"""
Logging configuration utilities for bot processes.

This module ensures that all bot-related processes use proper logging
configuration that writes to Cloud Logging instead of stderr.
"""

import logging
import logging.config

from django.conf import settings

# Global flag to prevent multiple initialization
_logging_initialized = False


def setup_bot_logging():
    """
    Set up proper logging configuration for bot processes.

    This function ensures that all log messages from bot processes
    are properly routed to Cloud Logging with correct severity levels,
    preventing them from being written to stderr and misclassified as errors.
    """
    global _logging_initialized

    # Only initialize once to prevent duplicate setup
    if _logging_initialized:
        return

    # Only configure logging if Django settings contain LOGGING
    if hasattr(settings, "LOGGING") and settings.LOGGING:
        try:
            # Clear existing handlers first to avoid conflicts
            root = logging.getLogger()
            for handler in list(root.handlers):
                root.removeHandler(handler)

            # Apply the Django logging configuration
            logging.config.dictConfig(settings.LOGGING)

            # Mark as initialized before logging to prevent recursion
            _logging_initialized = True

        except Exception as e:
            # Fallback to basic configuration if Django config fails
            import sys

            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                handlers=[logging.StreamHandler(sys.stdout)],  # Explicit stdout
            )
            _logging_initialized = True
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to apply Django logging config: {e}")
    else:
        # Fallback configuration when Django settings unavailable
        import sys

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)],  # Explicit stdout
        )
        _logging_initialized = True
        logger = logging.getLogger(__name__)
        logger.info("Using fallback logging configuration")


def get_bot_logger(name):
    """
    Get a properly configured logger for bot components.

    Args:
        name (str): Name for the logger (typically __name__)

    Returns:
        logging.Logger: Configured logger instance
    """
    # Ensure logging is set up
    setup_bot_logging()

    # Return logger with the specified name
    return logging.getLogger(name)
