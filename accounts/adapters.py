import logging
from urllib.parse import quote, urlsplit, urlunsplit

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


def is_crowdsec_bad_for_signup(result: dict) -> bool:
    try:
        scores = result.get("scores") or {}
        # An IP that has been quiet for a while has empty last_day scores, so fall back to
        # overall to catch known offenders whose activity is stale.
        windows = [scores.get("last_day") or {}, scores.get("overall") or {}]

        return result.get("reputation") == "malicious" or (bool(result.get("behaviors")) and any(window.get("threat", 0) >= 3 or window.get("aggressiveness", 0) >= 3 for window in windows))
    except Exception as exc:
        logger.warning(
            f"Could not interpret Crowdsec response {result}",
            exc_info=exc,
        )
        return False


def validate_ip_with_crowdsec(email: str, ip: str) -> None:
    if not ip or ip == "unknown":
        return

    try:
        response = requests.get(
            f"https://cti.api.crowdsec.net/v2/smoke/{ip}",
            headers={"x-api-key": settings.CROWDSEC_API_KEY},
            timeout=(2, 3),  # connect timeout, read timeout
        )
        # Crowdsec returns 404 for addresses it has never seen, which means nothing bad is known about them.
        if response.status_code == 404:
            logger.warning(f"Ignoring Crowdsec validation for unknown IP {ip}")
            return
        response.raise_for_status()
        result = response.json()
    except Exception as exc:
        logger.warning(
            f"Crowdsec IP validation failed for email {email} from ip {ip}",
            exc_info=exc,
        )
        return

    logger.info(f"Crowdsec IP validation response for email {email} from ip {ip}: {result}")

    if is_crowdsec_bad_for_signup(result):
        logger.warning(f"Blocking signup for email {email} from ip {ip} flagged by Crowdsec")
        raise ValidationError("We are unable to complete your sign up at this time.")


def validate_ip_with_cleantalk(email: str, ip: str) -> None:
    if not ip or ip == "unknown":
        return

    try:
        response = requests.get(
            "https://api.cleantalk.org/",
            params={
                "method_name": "spam_check",
                "auth_key": settings.CLEANTALK_API_KEY,
                "ip": ip,
            },
            timeout=(2, 3),
        )
        response.raise_for_status()
        result = response.json()

        if result.get("error_no"):
            raise ValueError(f"CleanTalk API error {result.get('error_no')}: {result.get('error_message')}")

        data = result.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected CleanTalk data: {data!r}")

        record = data.get(ip)
        if not isinstance(record, dict):
            raise ValueError(f"Missing CleanTalk result for IP {ip}")

    except Exception as exc:
        logger.warning(
            f"CleanTalk IP validation failed for email {email} from ip {ip}",
            exc_info=exc,
        )
        return

    logger.info(f"Cleantalk IP validation response for email {email} from ip {ip}: {record}")

    if str(record.get("appears")) == "1":
        logger.warning(f"Blocking signup for email {email} from ip {ip} flagged by Cleantalk")
        raise ValidationError("We are unable to complete your sign up at this time.")


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


def validate_email_with_usercheck(email: str) -> None:
    if settings.BYPASS_MAILGUN_VALIDATION_SUBSTRING and settings.BYPASS_MAILGUN_VALIDATION_SUBSTRING in email:
        return

    try:
        response = requests.get(
            f"https://api.usercheck.com/email/{quote(email)}",
            headers={"Authorization": f"Bearer {settings.USERCHECK_API_KEY}"},
            timeout=(3, 15),  # connect timeout, read timeout,
        )
        response.raise_for_status()
        validation = response.json()
    except Exception as exc:
        logger.warning(
            f"UserCheck email validation failed for email {email}",
            exc_info=exc,
        )
        return

    logger.info(f"UserCheck email validation response for email {email}: {validation}")

    if validation.get("disposable") or validation.get("relay_domain") or validation.get("free_subdomain"):
        raise ValidationError("Please use a permanent email address.")

    if validation.get("blocklisted") or validation.get("spam"):
        logger.warning(f"Blocking signup for email {email} flagged by UserCheck")
        raise ValidationError("We are unable to complete your sign up at this time.")

    # A domain with no MX records cannot receive our verification email.
    if validation.get("mx") is False:
        raise ValidationError("This email address does not appear to be valid.")


class StandardAccountAdapter(DefaultAccountAdapter):
    def clean_email(self, email: str) -> str:
        email = super().clean_email(email)

        if settings.CLEANTALK_API_KEY:
            validate_ip_with_cleantalk(email, get_request_ip())

        if settings.CROWDSEC_API_KEY:
            validate_ip_with_crowdsec(email, get_request_ip())

        if settings.USERCHECK_API_KEY:
            validate_email_with_usercheck(email)

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
