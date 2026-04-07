import os


def signup_status(request):
    """Expose signup open/closed status to templates."""
    disable_signup = os.getenv("DISABLE_SIGNUP") and os.getenv("DISABLE_SIGNUP") != "false"
    return {
        "SIGNUP_OPEN": not disable_signup,
    }
