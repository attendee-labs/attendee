from allauth.account.adapter import DefaultAccountAdapter
from django.contrib.auth import login
from django.urls import reverse


class StandardAccountAdapter(DefaultAccountAdapter):
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
