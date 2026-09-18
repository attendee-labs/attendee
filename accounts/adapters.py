import logging
from urllib.parse import urlsplit, urlunsplit

import requests
from allauth.account.adapter import DefaultAccountAdapter
from allauth.core import context
from django.conf import settings
from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.urls import reverse

logger = logging.getLogger(__name__)


def get_request_ip(request=None) -> str:
    # Callers that have the request (e.g. views) should pass it. Allauth hooks like
    # clean_email don't get one, so fall back to the contextvar allauth sets per request.
    if request is None:
        request = getattr(context, "request", None)
    if request is None:
        return "unknown"

    # We sit behind a proxy, so REMOTE_ADDR is the proxy's address. The first entry in
    # X-Forwarded-For is the client, the rest are the proxies it passed through.
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR") or "unknown"


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

    logger.info(f"Mailgun email validation response for email {email} from ip {get_request_ip()}: {validation}")

    if validation.get("is_disposable_address"):
        raise ValidationError("Please use a permanent email address.")

    result = validation.get("result")

    if result in {"undeliverable", "do_not_send", "unknown"}:
        raise ValidationError("This email address does not appear to be valid.")


class StandardAccountAdapter(DefaultAccountAdapter):
    def clean_email(self, email: str) -> str:
        email = super().clean_email(email)

        # Log the IP here, separately
        logger.info(f"Cleaning email {email} from ip {get_request_ip()}")

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

    # Ensure we use settings.SITE_DOMAIN for the URLs in emails
    def _use_site_domain(self, url):
        try:
            parsed = urlsplit(url)
        except ValueError:
            return url

        # Anything without both a scheme and a host isn't an absolute URL we can
        # swap the domain on, and rewriting it would corrupt the original value.
        if not parsed.scheme or not parsed.netloc:
            return url

        return urlunsplit(
            (
                parsed.scheme,
                settings.SITE_DOMAIN,
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )

    def send_mail(self, template_prefix, email, context):
        context = context.copy()

        for key, value in context.items():
            if key.endswith("_url") and isinstance(value, str):
                context[key] = self._use_site_domain(value)

        return super().send_mail(template_prefix, email, context)


class NoNewUsersAccountAdapter(StandardAccountAdapter):
    def is_open_for_signup(self, request):
        return False
