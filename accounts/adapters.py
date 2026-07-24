import logging

import requests
from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings
from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.urls import reverse

logger = logging.getLogger(__name__)


def validate_email_with_mailgun(email: str) -> None:
    if settings.BYPASS_MAILGUN_VALIDATION_SUBSTRING and settings.BYPASS_MAILGUN_VALIDATION_SUBSTRING in email:
        return

    try:
        response = requests.post(
            "https://api.mailgun.net/v4/address/validate",
            auth=("api", settings.MAILGUN_VALIDATION_API_KEY),
            data={"address": email},
            params={"provider_lookup": "true"},
            timeout=(3, 15),  # connect timeout, read timeout,
        )
        response.raise_for_status()
        validation = response.json()
    except Exception as exc:
        logger.warning(
            f"Mailgun email validation failed for email {email}",
            exc_info=exc,
        )
        return

    logger.info(f"Mailgun email validation response for email {email}: {validation}")

    if validation.get("is_disposable_address"):
        raise ValidationError("Please use a permanent email address.")

    result = validation.get("result")

    if result in {"undeliverable", "do_not_send", "unknown"}:
        raise ValidationError("This email address does not appear to be valid.")


class StandardAccountAdapter(DefaultAccountAdapter):
    def clean_email(self, email: str) -> str:
        email = super().clean_email(email)

        if settings.MAILGUN_VALIDATION_API_KEY:
            validate_email_with_mailgun(email)

        return email

    def get_email_verification_redirect_url(self, email_address):
        user = email_address.user
        if getattr(user, "invited_by", None):
            return reverse("account_set_password")
        return super().get_email_verification_redirect_url(email_address)

    def confirm_email(self, request, email_address):
        """
        Marks the given email address as confirmed on the db and logs in the user
        if they were invited by someone else.
        """
        # Call the parent method to handle the confirmation
        confirm_email_response = super().confirm_email(request, email_address)

        # Log in the user if they were invited and not already authenticated
        # Even though we set ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION to True, django will not log the user
        # in because they are coming from a different machine then the one that sent the email.
        user = email_address.user
        if user.invited_by and not request.user.is_authenticated:
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")

        return confirm_email_response


class NoNewUsersAccountAdapter(StandardAccountAdapter):
    def is_open_for_signup(self, request):
        return False
