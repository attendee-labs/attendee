import base64
import logging
import math
import os

import stripe
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import models
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic.list import ListView

from .models import (
    ApiKey,
    Bot,
    BotEvent,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    Credentials,
    CreditTransaction,
    Project,
    RecordingStates,
    Utterance,
    WebhookDeliveryAttempt,
    WebhookDeliveryAttemptStatus,
    WebhookSecret,
    WebhookSubscription,
    WebhookTriggerTypes,
)
from .stripe_utils import process_checkout_session_completed
from .utils import generate_recordings_json_for_bot_detail_view

logger = logging.getLogger(__name__)


class ProjectUrlContextMixin:
    def get_project_context(self, object_id, project):
        return {
            "project": project,
            "charge_credits_for_bots_setting": settings.CHARGE_CREDITS_FOR_BOTS,
        }


class ProjectDashboardView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        try:
            project = get_object_or_404(Project, object_id=object_id, organization=request.user.organization)
        except:
            return redirect("/")

        # Quick start guide status checks
        zoom_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).exists()

        deepgram_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.DEEPGRAM).exists()

        has_api_keys = ApiKey.objects.filter(project=project).exists()

        has_ended_bots = Bot.objects.filter(project=project, state=BotStates.ENDED).exists()

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "quick_start": {
                    "has_credentials": zoom_credentials and deepgram_credentials,
                    "has_api_keys": has_api_keys,
                    "has_ended_bots": has_ended_bots,
                }
            }
        )

        return render(request, "projects/project_dashboard.html", context)


class ProjectApiKeysView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_object_or_404(Project, object_id=object_id, organization=request.user.organization)
        context = self.get_project_context(object_id, project)
        context["api_keys"] = ApiKey.objects.filter(project=project).order_by("-created_at")
        return render(request, "projects/project_api_keys.html", context)


class CreateApiKeyView(LoginRequiredMixin, View):
    def post(self, request, object_id):
        project = get_object_or_404(Project, object_id=object_id, organization=request.user.organization)
        name = request.POST.get("name")

        if not name:
            return HttpResponse("Name is required", status=400)

        api_key_instance, api_key = ApiKey.create(project=project, name=name)

        # Render the success modal content
        return render(
            request,
            "projects/partials/api_key_created_modal.html",
            {"api_key": api_key, "name": name},
        )


class DeleteApiKeyView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def delete(self, request, object_id, key_object_id):
        api_key = get_object_or_404(
            ApiKey,
            object_id=key_object_id,
            project__organization=request.user.organization,
        )
        api_key.delete()
        context = self.get_project_context(object_id, api_key.project)
        context["api_keys"] = ApiKey.objects.filter(project=api_key.project).order_by("-created_at")
        return render(request, "projects/project_api_keys.html", context)


class RedirectToDashboardView(LoginRequiredMixin, View):
    def get(self, request, object_id, extra=None):
        return redirect("bots:project-dashboard", object_id=object_id)


class CreateCredentialsView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_object_or_404(Project, object_id=object_id, organization=request.user.organization)

        try:
            credential_type = int(request.POST.get("credential_type"))
            if credential_type not in [choice[0] for choice in Credentials.CredentialTypes.choices]:
                return HttpResponse("Invalid credential type", status=400)

            # Get or create the credential instance
            credential, created = Credentials.objects.get_or_create(project=project, credential_type=credential_type)

            # Parse the credentials data based on type
            if credential_type == Credentials.CredentialTypes.ZOOM_OAUTH:
                credentials_data = {
                    "client_id": request.POST.get("client_id"),
                    "client_secret": request.POST.get("client_secret"),
                }

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)

            elif credential_type == Credentials.CredentialTypes.DEEPGRAM:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.GLADIA:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.OPENAI:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.GOOGLE_TTS:
                credentials_data = {"service_account_json": request.POST.get("service_account_json")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            else:
                return HttpResponse("Unsupported credential type", status=400)

            # Store the encrypted credentials
            credential.set_credentials(credentials_data)

            # Return the entire settings page with updated context
            context = self.get_project_context(object_id, project)
            context["credentials"] = credential.get_credentials()
            context["credential_type"] = credential.credential_type
            if credential.credential_type == Credentials.CredentialTypes.ZOOM_OAUTH:
                return render(request, "projects/partials/zoom_credentials.html", context)
            elif credential.credential_type == Credentials.CredentialTypes.DEEPGRAM:
                return render(request, "projects/partials/deepgram_credentials.html", context)
            elif credential.credential_type == Credentials.CredentialTypes.GLADIA:
                return render(request, "projects/partials/gladia_credentials.html", context)
            elif credential.credential_type == Credentials.CredentialTypes.OPENAI:
                return render(request, "projects/partials/openai_credentials.html", context)
            elif credential.credential_type == Credentials.CredentialTypes.GOOGLE_TTS:
                return render(request, "projects/partials/google_tts_credentials.html", context)
            else:
                return HttpResponse("Cannot render the partial for this credential type", status=400)

        except Exception as e:
            return HttpResponse(str(e), status=400)


class ProjectCredentialsView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_object_or_404(Project, object_id=object_id, organization=request.user.organization)

        # Try to get existing credentials
        zoom_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).first()

        deepgram_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.DEEPGRAM).first()

        gladia_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.GLADIA).first()

        openai_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.OPENAI).first()

        google_tts_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.GOOGLE_TTS).first()

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "zoom_credentials": zoom_credentials.get_credentials() if zoom_credentials else None,
                "zoom_credential_type": Credentials.CredentialTypes.ZOOM_OAUTH,
                "deepgram_credentials": deepgram_credentials.get_credentials() if deepgram_credentials else None,
                "deepgram_credential_type": Credentials.CredentialTypes.DEEPGRAM,
                "google_tts_credentials": google_tts_credentials.get_credentials() if google_tts_credentials else None,
                "google_tts_credential_type": Credentials.CredentialTypes.GOOGLE_TTS,
                "gladia_credentials": gladia_credentials.get_credentials() if gladia_credentials else None,
                "gladia_credential_type": Credentials.CredentialTypes.GLADIA,
                "openai_credentials": openai_credentials.get_credentials() if openai_credentials else None,
                "openai_credential_type": Credentials.CredentialTypes.OPENAI,
            }
        )

        return render(request, "projects/project_credentials.html", context)


class ProjectBotsView(LoginRequiredMixin, ProjectUrlContextMixin, ListView):
    template_name = "projects/project_bots.html"
    context_object_name = "bots"
    paginate_by = 20

    def get_queryset(self):
        project = get_object_or_404(Project, object_id=self.kwargs["object_id"], organization=self.request.user.organization)

        # Start with the base queryset
        queryset = Bot.objects.filter(project=project)

        # Apply date filters if provided
        start_date = self.request.GET.get("start_date")
        end_date = self.request.GET.get("end_date")

        if start_date:
            queryset = queryset.filter(created_at__gte=start_date)
        if end_date:
            # Add 1 day to include the end date fully
            from datetime import datetime, timedelta

            try:
                end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")
                end_date_obj = end_date_obj + timedelta(days=1)
                queryset = queryset.filter(created_at__lt=end_date_obj)
            except (ValueError, TypeError):
                # Handle invalid date format
                pass

        # Apply state filters if provided
        states = self.request.GET.getlist("states")
        if states:
            # Convert string values to integers
            try:
                state_values = [int(state) for state in states if state.isdigit()]
                if state_values:
                    queryset = queryset.filter(state__in=state_values)
            except (ValueError, TypeError):
                # Handle invalid state values
                pass

        # Get the latest bot event type and subtype for each bot using subquery annotations
        latest_event_subquery_base = BotEvent.objects.filter(bot=models.OuterRef("pk")).order_by("-created_at")
        latest_event_type = latest_event_subquery_base.values("event_type")[:1]
        latest_event_sub_type = latest_event_subquery_base.values("event_sub_type")[:1]

        # Apply annotations and ordering
        queryset = queryset.annotate(last_event_type=models.Subquery(latest_event_type), last_event_sub_type=models.Subquery(latest_event_sub_type)).order_by("-created_at")

        # Add display names for the event types
        for bot in queryset:
            if bot.last_event_type:
                bot.last_event_type_display = dict(BotEventTypes.choices).get(bot.last_event_type, str(bot.last_event_type))
            if bot.last_event_sub_type:
                bot.last_event_sub_type_display = dict(BotEventSubTypes.choices).get(bot.last_event_sub_type, str(bot.last_event_sub_type))

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = get_object_or_404(Project, object_id=self.kwargs["object_id"], organization=self.request.user.organization)
        context.update(self.get_project_context(self.kwargs["object_id"], project))

        # Add BotStates for the template
        context["BotStates"] = BotStates

        # Add filter parameters to context for maintaining state
        context["filter_params"] = {"start_date": self.request.GET.get("start_date", ""), "end_date": self.request.GET.get("end_date", ""), "states": self.request.GET.getlist("states")}

        return context


class ProjectBotDetailView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id, bot_object_id):
        project = get_object_or_404(Project, object_id=object_id, organization=request.user.organization)

        bot = get_object_or_404(Bot, object_id=bot_object_id, project=project)

        # Prefetch recordings with their utterances and participants
        bot.recordings.all().prefetch_related(models.Prefetch("utterances", queryset=Utterance.objects.select_related("participant")))

        # Prefetch bot events with their debug screenshots
        bot.bot_events.prefetch_related("debug_screenshots")

        # Get webhook delivery attempts for this bot
        webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(bot=bot).select_related("webhook_subscription").order_by("-created_at")

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "bot": bot,
                "BotStates": BotStates,
                "RecordingStates": RecordingStates,
                "recordings": generate_recordings_json_for_bot_detail_view(bot),
                "webhook_delivery_attempts": webhook_delivery_attempts,
                "WebhookDeliveryAttemptStatus": WebhookDeliveryAttemptStatus,
                "credits_consumed": -sum([t.credits_delta() for t in bot.credit_transactions.all()]) if bot.credit_transactions.exists() else None,
            }
        )

        return render(request, "projects/project_bot_detail.html", context)


class ProjectWebhooksView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_object_or_404(Project, object_id=object_id, organization=request.user.organization)
        context = self.get_project_context(object_id, project)
        context["webhooks"] = WebhookSubscription.objects.filter(project=project).order_by("-created_at")
        context["webhook_options"] = [trigger_type for trigger_type in WebhookTriggerTypes]
        return render(request, "projects/project_webhooks.html", context)


class CreateWebhookView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_object_or_404(Project, object_id=object_id, organization=request.user.organization)
        url = request.POST.get("url")
        triggers = request.POST.getlist("triggers[]")

        # Check if URL is valid
        if not url.startswith("https://"):
            return HttpResponse("URL must start with https://", status=400)
        if WebhookSubscription.objects.filter(url=url, project=project).exists():
            return HttpResponse("URL already subscribed", status=400)
        # There is a limit of 2 webhooks per projects for now
        if WebhookSubscription.objects.filter(project=project).count() >= 2:
            return HttpResponse("You have reached the maximum number of webhooks", status=400)

        # Check the event is subscribable
        subscribed_triggers = [int(x) for x in triggers]
        for trigger in subscribed_triggers:
            if trigger not in [trigger.value for trigger in WebhookTriggerTypes]:
                return HttpResponse(f"Invalid event type: {trigger}", status=400)

        # Get the project's secret for the webhook subscription. If new project, create a new one
        webhook_secret, created = WebhookSecret.objects.get_or_create(project=project)

        # Create the webhook subscription
        WebhookSubscription.objects.create(
            project=project,
            url=url,
            triggers=subscribed_triggers,
        )

        # Render the success modal content
        return render(
            request,
            "projects/partials/webhook_subscription_created_modal.html",
            {
                "secret": base64.b64encode(webhook_secret.get_secret()).decode("utf-8"),
                "url": url,
                "triggers": [WebhookTriggerTypes.trigger_type_to_api_code(x) for x in subscribed_triggers],
            },
        )


class DeleteWebhookView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def delete(self, request, object_id, webhook_object_id):
        webhook = get_object_or_404(
            WebhookSubscription,
            object_id=webhook_object_id,
            project__organization=request.user.organization,
        )
        webhook.delete()
        context = self.get_project_context(object_id, webhook.project)
        context["webhooks"] = WebhookSubscription.objects.filter(project=webhook.project).order_by("-created_at")
        context["webhook_options"] = [trigger_type for trigger_type in WebhookTriggerTypes]
        return render(request, "projects/project_webhooks.html", context)


class ProjectBillingView(LoginRequiredMixin, ProjectUrlContextMixin, ListView):
    template_name = "projects/project_billing.html"
    context_object_name = "transactions"
    paginate_by = 20

    def get_queryset(self):
        project = get_object_or_404(Project, object_id=self.kwargs["object_id"], organization=self.request.user.organization)
        return CreditTransaction.objects.filter(organization=project.organization).order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = get_object_or_404(Project, object_id=self.kwargs["object_id"], organization=self.request.user.organization)
        context.update(self.get_project_context(self.kwargs["object_id"], project))
        return context


class CheckoutSuccessView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        session_id = request.GET.get("session_id")
        if not session_id:
            return HttpResponse("No session ID provided", status=400)

        # Retrieve the session details
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id, api_key=os.getenv("STRIPE_SECRET_KEY"))
        except Exception as e:
            return HttpResponse(f"Error retrieving session details: {e}", status=400)

        process_checkout_session_completed(checkout_session)

        return redirect(reverse("bots:project-billing", kwargs={"object_id": object_id}))


class CreateCheckoutSessionView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        # Get the purchase amount from the form submission
        try:
            purchase_amount = float(request.POST.get("purchase_amount", 50.0))
            if purchase_amount < 1:
                purchase_amount = 1.0
        except (ValueError, TypeError):
            purchase_amount = 50.0  # Default fallback

        # Calculate credits based on tiered pricing
        if purchase_amount <= 200:
            # Tier 1: $0.50 per credit
            credit_amount = purchase_amount / 0.5
        elif purchase_amount <= 1000:
            # Tier 2: $0.40 per credit
            credit_amount = purchase_amount / 0.4
        else:
            # Tier 3: $0.35 per credit
            credit_amount = purchase_amount / 0.35

        # Floor the credit amount to ensure whole credits
        credit_amount = math.floor(credit_amount)

        # Ensure at least 1 credit
        if credit_amount < 1:
            credit_amount = 1

        # Convert purchase amount to cents for Stripe
        unit_amount = int(purchase_amount * 100)  # in cents

        if unit_amount > 1000000:  # $10000 limit
            return HttpResponse("The maximum purchase amount is $10000.", status=400)

        # Create checkout session
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"{credit_amount} Attendee Credits",
                            "description": f"Purchase {credit_amount} Attendee credits for your account",
                        },
                        "unit_amount": unit_amount,
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=request.build_absolute_uri(reverse("bots:checkout-success", kwargs={"object_id": object_id})) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.build_absolute_uri(reverse("bots:project-billing", kwargs={"object_id": object_id})),
            metadata={
                "organization_id": str(request.user.organization.id),
                "user_id": str(request.user.id),
                "credit_amount": str(credit_amount),
            },
            api_key=os.getenv("STRIPE_SECRET_KEY"),
        )

        # Redirect directly to the Stripe checkout page
        return redirect(checkout_session.url)
