import base64
import json
import logging
import os
import uuid

import stripe
from allauth.account.utils import send_email_confirmation
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.http import HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import ListView

from accounts.models import User, UserRole

from .bots_api_utils import BotCreationSource, create_bot, create_webhook_subscription
from .launch_bot_utils import launch_bot
from .models import (
    ApiKey,
    Bot,
    BotEvent,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    Calendar,
    CalendarEvent,
    CalendarPlatform,
    CalendarStates,
    ChatMessage,
    Credentials,
    CreditTransaction,
    GoogleMeetBotLogin,
    GoogleMeetBotLoginGroup,
    Participant,
    ParticipantEventTypes,
    Project,
    ProjectAccess,
    Recording,
    RecordingStates,
    RecordingTranscriptionStates,
    RecordingTypes,
    Utterance,
    WebhookDeliveryAttempt,
    WebhookDeliveryAttemptStatus,
    WebhookSecret,
    WebhookSubscription,
    WebhookTriggerTypes,
    ZoomOAuthApp,
)
from .stripe_utils import credit_amount_for_purchase_amount_dollars, process_checkout_session_completed
from .utils import generate_recordings_json_for_bot_detail_view
from .zoom_oauth_apps_api_utils import create_or_update_zoom_oauth_app

logger = logging.getLogger(__name__)


def get_project_for_user(user, project_object_id):
    project = get_object_or_404(Project, object_id=project_object_id, organization=user.organization)
    # If you're an admin you can access any project in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=project, user=user).exists():
        raise PermissionDenied
    return project


def get_webhook_subscription_for_user(user, webhook_subscription_object_id):
    webhook_subscription = get_object_or_404(WebhookSubscription, object_id=webhook_subscription_object_id, project__organization=user.organization)
    # If you're an admin you can access any webhook subscription in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=webhook_subscription.project, user=user).exists():
        raise PermissionDenied
    return webhook_subscription


def get_api_key_for_user(user, api_key_object_id):
    api_key = get_object_or_404(ApiKey, object_id=api_key_object_id, project__organization=user.organization)
    # If you're an admin you can access any api key in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=api_key.project, user=user).exists():
        raise PermissionDenied
    return api_key


def get_calendar_for_user(user, calendar_object_id):
    calendar = get_object_or_404(Calendar, object_id=calendar_object_id, project__organization=user.organization)
    # If you're an admin you can access any calendar in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=calendar.project, user=user).exists():
        raise PermissionDenied
    return calendar


def get_calendar_event_for_user(user, calendar_event_object_id):
    calendar_event = get_object_or_404(CalendarEvent, object_id=calendar_event_object_id, calendar__project__organization=user.organization)
    # If you're an admin you can access any calendar event in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=calendar_event.calendar.project, user=user).exists():
        raise PermissionDenied
    return calendar_event


def get_google_meet_bot_login_for_user(user, google_meet_bot_login_object_id):
    google_meet_bot_login = get_object_or_404(GoogleMeetBotLogin, object_id=google_meet_bot_login_object_id, group__project__organization=user.organization)
    # If you're an admin you can access any Google Meet bot login in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=google_meet_bot_login.group.project, user=user).exists():
        raise PermissionDenied
    return google_meet_bot_login


def get_webhook_options_for_project(project):
    trigger_types = [trigger_type for trigger_type in WebhookTriggerTypes]
    if not project.organization.is_managed_zoom_oauth_enabled:
        trigger_types.remove(WebhookTriggerTypes.ZOOM_OAUTH_CONNECTION_STATE_CHANGE)
    if not project.organization.is_async_transcription_enabled:
        trigger_types.remove(WebhookTriggerTypes.ASYNC_TRANSCRIPTION_STATE_CHANGE)
    return trigger_types


def get_partial_for_credential_type(credential_type, request, context):
    if credential_type == Credentials.CredentialTypes.ZOOM_OAUTH:
        return render(request, "projects/partials/zoom_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.DEEPGRAM:
        return render(request, "projects/partials/deepgram_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.GLADIA:
        return render(request, "projects/partials/gladia_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.OPENAI:
        return render(request, "projects/partials/openai_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.GOOGLE_TTS:
        return render(request, "projects/partials/google_tts_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.ASSEMBLY_AI:
        return render(request, "projects/partials/assembly_ai_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.SARVAM:
        return render(request, "projects/partials/sarvam_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.ELEVENLABS:
        return render(request, "projects/partials/elevenlabs_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.TEAMS_BOT_LOGIN:
        return render(request, "projects/partials/teams_bot_login_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.KYUTAI:
        return render(request, "projects/partials/kyutai_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE:
        return render(request, "projects/partials/external_media_storage_credentials.html", context)
    else:
        return HttpResponse("Cannot render the partial for this credential type", status=400)


class AdminRequiredMixin(LoginRequiredMixin):
    """
    Mixin for class-based views that can only be accessed by admin users.
    Inherits from LoginRequiredMixin to ensure user is authenticated first.
    """

    def dispatch(self, request, *args, **kwargs):
        # First check if user is authenticated (handled by LoginRequiredMixin)
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        # Then check if user is admin
        if request.user.role != UserRole.ADMIN:
            raise PermissionDenied("Only administrators can access this resource.")

        return super().dispatch(request, *args, **kwargs)


class ProjectUrlContextMixin:
    def get_project_context(self, object_id, project):
        return {
            "project": project,
            "charge_credits_for_bots_setting": settings.CHARGE_CREDITS_FOR_BOTS,
            "user_projects": Project.accessible_to(self.request.user),
            "UserRole": UserRole,
            "debug_mode": True if settings.DEBUG else False,
        }


class ProjectDashboardView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        try:
            project = get_project_for_user(user=request.user, project_object_id=object_id)
        except:
            return redirect("/")

        # Quick start guide status checks
        zoom_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).exists()

        deepgram_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.DEEPGRAM).exists()

        has_api_keys = ApiKey.objects.filter(project=project).exists()

        has_ended_bots = Bot.objects.filter(project=project, state=BotStates.ENDED).exists()

        has_created_bots_via_api = BotEvent.objects.filter(bot__project=project, event_type=BotEventTypes.JOIN_REQUESTED, metadata__source=BotCreationSource.API).exists()

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "quick_start": {
                    "has_credentials": zoom_credentials and deepgram_credentials,
                    "has_api_keys": has_api_keys,
                    "has_ended_bots": has_ended_bots,
                    "has_created_bots_via_api": has_created_bots_via_api,
                },
            }
        )

        return render(request, "projects/project_dashboard.html", context)


class ProjectApiKeysView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        context = self.get_project_context(object_id, project)
        context["api_keys"] = ApiKey.objects.filter(project=project).order_by("-created_at")
        return render(request, "projects/project_api_keys.html", context)


class CreateApiKeyView(LoginRequiredMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
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
        api_key = get_api_key_for_user(user=request.user, api_key_object_id=key_object_id)
        api_key.delete()
        context = self.get_project_context(object_id, api_key.project)
        context["api_keys"] = ApiKey.objects.filter(project=api_key.project).order_by("-created_at")
        return render(request, "projects/project_api_keys.html", context)


class RedirectToDashboardView(LoginRequiredMixin, View):
    def get(self, request, object_id, extra=None):
        return redirect("bots:project-dashboard", object_id=object_id)


class DeleteZoomOAuthAppView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        zoom_oauth_app = ZoomOAuthApp.objects.filter(project=project).first()
        if not zoom_oauth_app:
            return HttpResponse("Zoom OAuth app not found", status=404)
        zoom_oauth_app.delete()
        context = self.get_project_context(object_id, project)
        return render(request, "projects/partials/zoom_oauth_app.html", context)


class CreateZoomOAuthAppView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        zoom_oauth_app, error = create_or_update_zoom_oauth_app(
            project=project,
            client_id=request.POST.get("client_id"),
            client_secret=request.POST.get("client_secret"),
            webhook_secret=request.POST.get("webhook_secret"),
        )

        if error:
            return HttpResponse(error, status=400)

        context = self.get_project_context(object_id, project)
        context["zoom_oauth_app"] = zoom_oauth_app
        return render(request, "projects/partials/zoom_oauth_app.html", context)


class CreateCredentialsView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

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
            elif credential_type == Credentials.CredentialTypes.ASSEMBLY_AI:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.SARVAM:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.ELEVENLABS:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.KYUTAI:
                credentials_data = {
                    "server_url": request.POST.get("server_url"),
                }
                # Only include api_key if it's provided
                api_key = request.POST.get("api_key")
                if api_key:
                    credentials_data["api_key"] = api_key

                if not credentials_data.get("server_url"):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.GOOGLE_TTS:
                credentials_data = {"service_account_json": request.POST.get("service_account_json")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.TEAMS_BOT_LOGIN:
                credentials_data = {"username": request.POST.get("username"), "password": request.POST.get("password")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE:
                credentials_data = {"access_key_id": request.POST.get("access_key_id"), "access_key_secret": request.POST.get("access_key_secret"), "endpoint_url": request.POST.get("endpoint_url"), "region_name": request.POST.get("region_name")}

                if not credentials_data.get("access_key_id") or not credentials_data.get("access_key_secret") or (not credentials_data.get("endpoint_url") and not credentials_data.get("region_name")):
                    return HttpResponse("Missing required credentials data", status=400)
            else:
                return HttpResponse("Unsupported credential type", status=400)

            # Store the encrypted credentials
            credential.set_credentials(credentials_data)

            # Return the entire settings page with updated context
            context = self.get_project_context(object_id, project)
            context["credentials"] = credential.get_credentials()
            context["credential_type"] = credential.credential_type

            # Render the appropriate partial based on credential type
            return get_partial_for_credential_type(credential.credential_type, request, context)

        except Exception as e:
            return HttpResponse(str(e), status=400)


class DeleteCredentialsView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        try:
            credential_type = int(request.POST.get("credential_type"))
            if credential_type not in [choice[0] for choice in Credentials.CredentialTypes.choices]:
                return HttpResponse("Invalid credential type", status=400)

            # Find and delete the credential
            credential = Credentials.objects.filter(project=project, credential_type=credential_type).first()

            if credential:
                credential.delete()

            # Return the updated partial for the specific credential type
            context = self.get_project_context(object_id, project)
            context["credentials"] = None
            context["credential_type"] = credential_type

            # Render the appropriate partial based on credential type
            return get_partial_for_credential_type(credential_type, request, context)

        except Exception as e:
            error_id = str(uuid.uuid4())
            logger.error(f"Error deleting credentials (error_id={error_id}): {e}")
            return HttpResponse(f"Error deleting credentials. Error ID: {error_id}", status=400)


class ProjectCredentialsView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        # Try to get existing zoom oauth app
        zoom_oauth_app = ZoomOAuthApp.objects.filter(project=project).first()

        # Try to get existing google meet bot login group
        google_meet_bot_login_group = GoogleMeetBotLoginGroup.objects.filter(project=project).first()

        # Try to get existing credentials
        zoom_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).first()

        deepgram_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.DEEPGRAM).first()

        gladia_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.GLADIA).first()

        openai_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.OPENAI).first()

        google_tts_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.GOOGLE_TTS).first()

        assembly_ai_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.ASSEMBLY_AI).first()

        sarvam_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.SARVAM).first()

        elevenlabs_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.ELEVENLABS).first()

        kyutai_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.KYUTAI).first()

        teams_bot_login_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.TEAMS_BOT_LOGIN).first()

        external_media_storage_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE).first()

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "zoom_oauth_app": zoom_oauth_app,
                "google_meet_bot_login_group": google_meet_bot_login_group,
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
                "assembly_ai_credentials": assembly_ai_credentials.get_credentials() if assembly_ai_credentials else None,
                "assembly_ai_credential_type": Credentials.CredentialTypes.ASSEMBLY_AI,
                "sarvam_credentials": sarvam_credentials.get_credentials() if sarvam_credentials else None,
                "sarvam_credential_type": Credentials.CredentialTypes.SARVAM,
                "elevenlabs_credentials": elevenlabs_credentials.get_credentials() if elevenlabs_credentials else None,
                "elevenlabs_credential_type": Credentials.CredentialTypes.ELEVENLABS,
                "kyutai_credentials": kyutai_credentials.get_credentials() if kyutai_credentials else None,
                "kyutai_credential_type": Credentials.CredentialTypes.KYUTAI,
                "teams_bot_login_credentials": teams_bot_login_credentials.get_credentials() if teams_bot_login_credentials else None,
                "teams_bot_login_credential_type": Credentials.CredentialTypes.TEAMS_BOT_LOGIN,
                "external_media_storage_credentials": external_media_storage_credentials.get_credentials() if external_media_storage_credentials else None,
                "external_media_storage_credential_type": Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE,
            }
        )

        return render(request, "projects/project_credentials.html", context)


class ProjectBotsView(LoginRequiredMixin, ProjectUrlContextMixin, ListView):
    template_name = "projects/project_bots.html"
    context_object_name = "bots"
    paginate_by = 20

    def get_queryset(self):
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])

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

        # Apply search filter if provided
        search_query = self.request.GET.get("search", "").strip()
        if search_query:
            queryset = queryset.filter(models.Q(object_id__icontains=search_query) | models.Q(meeting_url__icontains=search_query) | models.Q(name__icontains=search_query))

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
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])
        context.update(self.get_project_context(self.kwargs["object_id"], project))

        # Add BotStates for the template
        context["BotStates"] = BotStates

        # Add filter parameters to context for maintaining state
        context["filter_params"] = {"start_date": self.request.GET.get("start_date", ""), "end_date": self.request.GET.get("end_date", ""), "states": self.request.GET.getlist("states"), "search": self.request.GET.get("search", "")}

        # Add flag to detect if create modal should be automatically opened
        context["open_create_modal"] = self.request.GET.get("open_create_modal") == "true"

        # Check if any bots in the current page have a join_at value
        context["has_scheduled_bots"] = any(bot.join_at is not None for bot in context["bots"])

        return context


class ProjectCalendarsView(LoginRequiredMixin, ProjectUrlContextMixin, ListView):
    template_name = "projects/project_calendars.html"
    context_object_name = "calendars"
    paginate_by = 20

    def get_queryset(self):
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])

        # Start with the base queryset
        queryset = Calendar.objects.filter(project=project)

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

        # Apply deduplication key filter if provided
        deduplication_key = self.request.GET.get("deduplication_key")
        if deduplication_key:
            # Filter for calendars with specific deduplication key
            queryset = queryset.filter(deduplication_key__icontains=deduplication_key)

        # Order by most recently created
        queryset = queryset.order_by("-created_at")

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])
        context.update(self.get_project_context(self.kwargs["object_id"], project))

        # Add CalendarStates and CalendarPlatform for the template
        context["CalendarStates"] = CalendarStates
        context["CalendarPlatform"] = CalendarPlatform

        # Add filter parameters to context for maintaining state
        context["filter_params"] = {
            "start_date": self.request.GET.get("start_date", ""),
            "end_date": self.request.GET.get("end_date", ""),
            "states": self.request.GET.getlist("states"),
            "deduplication_key": self.request.GET.get("deduplication_key", ""),
        }

        return context


class ProjectCalendarDetailView(LoginRequiredMixin, ProjectUrlContextMixin, ListView):
    template_name = "projects/project_calendar_detail.html"
    context_object_name = "calendar_events"
    paginate_by = 20

    def get_calendar(self):
        """Get the calendar object, cached for multiple calls"""
        if not hasattr(self, "_calendar"):
            try:
                self._calendar = get_calendar_for_user(user=self.request.user, calendar_object_id=self.kwargs["calendar_object_id"])
            except PermissionDenied:
                self._calendar = None
        return self._calendar

    def get_queryset(self):
        calendar = self.get_calendar()
        if not calendar:
            return []

        # Get calendar events for this calendar, ordered by start time (most recent first)
        return calendar.events.all().order_by("-start_time")

    def get(self, request, object_id, calendar_object_id):
        # Check if calendar exists, if not redirect
        calendar = self.get_calendar()
        if not calendar:
            return redirect("bots:project-calendars", object_id=object_id)

        # Check if project from url is the same as the calendar's project
        if calendar.project.object_id != object_id:
            return redirect("bots:project-calendars", object_id=object_id)

        # Continue with normal ListView processing
        return super().get(request, object_id, calendar_object_id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        calendar = self.get_calendar()
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])

        # Get webhook delivery attempts for this calendar (from calendar-related webhook subscriptions)
        webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(calendar=calendar).select_related("webhook_subscription").order_by("-created_at")

        context.update(self.get_project_context(self.kwargs["object_id"], project))
        context.update(
            {
                "calendar": calendar,
                "CalendarStates": CalendarStates,
                "CalendarPlatform": CalendarPlatform,
                "webhook_delivery_attempts": webhook_delivery_attempts,
                "WebhookDeliveryAttemptStatus": WebhookDeliveryAttemptStatus,
            }
        )

        return context


class ProjectCalendarEventDetailView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id, calendar_object_id, event_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        calendar_event = get_calendar_event_for_user(user=request.user, calendar_event_object_id=event_object_id)

        # Verify the calendar event belongs to the specified calendar
        if calendar_event.calendar.object_id != calendar_object_id:
            return redirect("bots:project-calendar-detail", object_id=object_id, calendar_object_id=calendar_object_id)

        # Check if project from url is the same as the calendar's project
        if calendar_event.calendar.project.object_id != object_id:
            return redirect("bots:project-calendar-detail", object_id=object_id, calendar_object_id=calendar_object_id)

        # Get any bots that were created for this calendar event
        bots_for_event = Bot.objects.filter(calendar_event=calendar_event).order_by("-created_at")

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "calendar": calendar_event.calendar,
                "calendar_event": calendar_event,
                "bots_for_event": bots_for_event,
                "CalendarStates": CalendarStates,
                "CalendarPlatform": CalendarPlatform,
                "BotStates": BotStates,
            }
        )

        return render(request, "projects/project_calendar_event_detail.html", context)


class ProjectBotDetailView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id, bot_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        try:
            bot = (
                Bot.objects.select_related()
                .prefetch_related(
                    "bot_events__debug_screenshots",
                )
                .get(object_id=bot_object_id, project=project)
            )
        except Bot.DoesNotExist:
            # Redirect to bots list if bot not found
            return redirect("bots:project-bots", object_id=object_id)

        # Get webhook delivery attempts for this bot (from both project-level and bot-specific webhook subscriptions)
        webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(bot=bot).select_related("webhook_subscription").order_by("-created_at")

        # Get chat messages for this bot
        chat_messages = ChatMessage.objects.filter(bot=bot).select_related("participant").order_by("created_at")

        # Get participants and participant events for this bot
        participants = Participant.objects.filter(bot=bot, is_the_bot=False).prefetch_related("events").order_by("created_at")

        # Get resource snapshots for this bot
        resource_snapshots = bot.resource_snapshots.all().order_by("created_at")

        # Calculate maximum values from resource snapshots
        max_ram_usage = 0
        max_cpu_usage = 0
        if resource_snapshots.exists():
            for snapshot in resource_snapshots:
                data = snapshot.data
                ram_usage = data.get("ram_usage_megabytes", 0)
                cpu_usage = data.get("cpu_usage_millicores", 0)

                if ram_usage > max_ram_usage:
                    max_ram_usage = ram_usage
                if cpu_usage > max_cpu_usage:
                    max_cpu_usage = cpu_usage

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "bot": bot,
                "BotStates": BotStates,
                "webhook_delivery_attempts": webhook_delivery_attempts,
                "chat_messages": chat_messages,
                "participants": participants,
                "ParticipantEventTypes": ParticipantEventTypes,
                "WebhookDeliveryAttemptStatus": WebhookDeliveryAttemptStatus,
                "credits_consumed": -sum([t.credits_delta() for t in bot.credit_transactions.all()]) if bot.credit_transactions.exists() else None,
                "resource_snapshots": resource_snapshots,
                "max_ram_usage": max_ram_usage,
                "max_cpu_usage": max_cpu_usage,
            }
        )

        return render(request, "projects/project_bot_detail.html", context)


class ProjectBotRecordingsView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id, bot_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        try:
            bot = (
                Bot.objects.select_related()
                .prefetch_related(
                    models.Prefetch(
                        "recordings",
                        queryset=Recording.objects.prefetch_related(
                            models.Prefetch(
                                "utterances",
                                queryset=Utterance.objects.select_related("participant"),
                            ),
                        ),
                    ),
                )
                .get(object_id=bot_object_id, project=project)
            )
        except Bot.DoesNotExist:
            # Redirect to bots list if bot not found
            return redirect("bots:project-bots", object_id=object_id)

        context = {
            "RecordingStates": RecordingStates,
            "RecordingTypes": RecordingTypes,
            "RecordingTranscriptionStates": RecordingTranscriptionStates,
            "recordings": generate_recordings_json_for_bot_detail_view(bot),
        }

        return render(request, "projects/partials/project_bot_recordings.html", context)


class ProjectWebhooksView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        # Get or create webhook secret for the project
        webhook_secret, created = WebhookSecret.objects.get_or_create(project=project)

        context = self.get_project_context(object_id, project)
        # Only show project-level webhooks, not bot-level ones
        context["webhooks"] = project.webhook_subscriptions.filter(bot__isnull=True).order_by("-created_at")
        context["webhook_options"] = get_webhook_options_for_project(project)
        context["webhook_secret"] = base64.b64encode(webhook_secret.get_secret()).decode("utf-8")
        context["REQUIRE_HTTPS_WEBHOOKS"] = settings.REQUIRE_HTTPS_WEBHOOKS
        return render(request, "projects/project_webhooks.html", context)


class ProjectProjectView(AdminRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        context = self.get_project_context(object_id, project)
        context["users_with_access"] = project.users_with_access()
        return render(request, "projects/project_project.html", context)


class ProjectTeamView(AdminRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        # Get all users in the organization with invited_by data and their project access
        users = request.user.organization.users.select_related("invited_by").prefetch_related("project_accesses__project").order_by("-is_active", "id")

        context = self.get_project_context(object_id, project)
        context["users"] = users
        # Needed for the checkbox list for choosing which products a user can access
        context["projects"] = request.user.organization.projects.all()
        return render(request, "projects/project_team.html", context)


class EditUserView(AdminRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        user_object_id = request.POST.get("user_object_id")
        is_admin = request.POST.get("is_admin") == "true"
        is_active = request.POST.get("is_active") == "true"
        selected_project_ids = request.POST.getlist("project_access")

        if not user_object_id:
            return HttpResponse("User ID is required", status=400)

        # Get the user to be edited
        user_to_edit = get_object_or_404(User, object_id=user_object_id, organization=request.user.organization)

        # Prevent editing yourself
        if user_to_edit.id == request.user.id:
            return HttpResponse("You cannot edit your own account", status=400)

        # Validate project selection for regular users
        if not is_admin and not selected_project_ids:
            return HttpResponse("Please select at least one project for regular users", status=400)

        # Validate that selected projects exist and belong to the organization
        if not is_admin and selected_project_ids:
            valid_projects = Project.objects.filter(object_id__in=selected_project_ids, organization=request.user.organization)
            if len(valid_projects) != len(selected_project_ids):
                return HttpResponse("Invalid project selection", status=400)

        try:
            with transaction.atomic():
                # Update user role
                user_role = UserRole.ADMIN if is_admin else UserRole.REGULAR_USER
                user_to_edit.role = user_role

                # Update user active status
                user_to_edit.is_active = is_active

                user_to_edit.save()

                # Update project access for regular users
                if not is_admin:
                    # Remove all existing project access
                    ProjectAccess.objects.filter(user=user_to_edit).delete()

                    # Add new project access entries
                    for project_id in selected_project_ids:
                        project_obj = Project.objects.get(object_id=project_id, organization=request.user.organization)
                        ProjectAccess.objects.create(project=project_obj, user=user_to_edit)
                else:
                    # If user is now admin, remove all project access entries
                    # since admins have access to all projects
                    ProjectAccess.objects.filter(user=user_to_edit).delete()

                # Return success response
                status_text = "active" if is_active else "disabled"
                role_text = "administrator" if is_admin else "regular user"
                return HttpResponse(f"User {user_to_edit.email} has been updated successfully. Role: {role_text}, Status: {status_text}.", status=200)

        except Exception as e:
            logger.error(f"Error updating user: {str(e)}")
            return HttpResponse("An error occurred while updating the user", status=500)


class InviteUserView(AdminRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        context = self.get_project_context(object_id, project)
        return render(request, "projects/project_team.html", context)

    def post(self, request, object_id):
        get_project_for_user(user=request.user, project_object_id=object_id)
        email = request.POST.get("email")
        is_admin = request.POST.get("is_admin") == "true"
        selected_project_ids = request.POST.getlist("project_access")

        if not email:
            return HttpResponse("Email is required", status=400)

        # Check if user already exists
        if User.objects.filter(email=email).exists():
            return HttpResponse("A user with this email already exists", status=400)

        # Validate project selection for regular users
        if not is_admin and not selected_project_ids:
            return HttpResponse("Please select at least one project for regular users", status=400)

        # Validate that selected projects exist and belong to the organization
        if not is_admin and selected_project_ids:
            valid_projects = Project.objects.filter(object_id__in=selected_project_ids, organization=request.user.organization)
            if len(valid_projects) != len(selected_project_ids):
                return HttpResponse("Invalid project selection", status=400)

        try:
            with transaction.atomic():
                # Create the user with appropriate role
                user_role = UserRole.ADMIN if is_admin else UserRole.REGULAR_USER
                user = User.objects.create_user(
                    email=email,
                    username=str(uuid.uuid4()),
                    organization=request.user.organization,
                    invited_by=request.user,
                    is_active=True,
                    role=user_role,
                )

                # Create project access entries for regular users
                if not is_admin and selected_project_ids:
                    for project_id in selected_project_ids:
                        project = Project.objects.get(object_id=project_id, organization=request.user.organization)
                        ProjectAccess.objects.create(project=project, user=user)

                # Send verification email
                send_email_confirmation(request, user, email=email)

                # Return success response
                return HttpResponse("Invitation sent successfully", status=200)

        except Exception as e:
            logger.error(f"Error creating invited user: {str(e)}")
            return HttpResponse("An error occurred while sending the invitation", status=500)


class CreateWebhookView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        url = request.POST.get("url")
        triggers = request.POST.getlist("triggers[]")

        # Create webhook subscription using shared function
        try:
            create_webhook_subscription(url, triggers, project, bot=None)
        except ValidationError as e:
            return HttpResponse(e.messages[0], status=400)

        # Get the project's webhook secret for response
        webhook_secret = WebhookSecret.objects.get(project=project)

        return render(
            request,
            "projects/partials/webhook_subscription_created_modal.html",
            {
                "secret": base64.b64encode(webhook_secret.get_secret()).decode("utf-8"),
                "url": url,
                "triggers": triggers,
            },
        )


class DeleteWebhookView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def delete(self, request, object_id, webhook_object_id):
        webhook = get_webhook_subscription_for_user(user=request.user, webhook_subscription_object_id=webhook_object_id)
        webhook.delete()
        context = self.get_project_context(object_id, webhook.project)
        context["webhooks"] = WebhookSubscription.objects.filter(project=webhook.project, bot__isnull=True).order_by("-created_at")
        context["webhook_options"] = get_webhook_options_for_project(webhook.project)
        context["REQUIRE_HTTPS_WEBHOOKS"] = settings.REQUIRE_HTTPS_WEBHOOKS
        return render(request, "projects/project_webhooks.html", context)


class ProjectBillingView(AdminRequiredMixin, ProjectUrlContextMixin, ListView):
    template_name = "projects/project_billing.html"
    context_object_name = "transactions"
    paginate_by = 20

    def get_queryset(self):
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])
        return CreditTransaction.objects.filter(organization=project.organization).order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])
        context.update(self.get_project_context(self.kwargs["object_id"], project))

        # Check if organization has a valid payment method
        has_payment_method = False
        if project.organization.autopay_stripe_customer_id:
            try:
                # Retrieve the customer to check for default payment method
                customer = stripe.Customer.retrieve(
                    project.organization.autopay_stripe_customer_id,
                    api_key=os.getenv("STRIPE_SECRET_KEY"),
                )
                # Check if customer has a default payment method
                has_payment_method = customer.invoice_settings.default_payment_method is not None
            except stripe.error.StripeError:
                # If there's an error querying Stripe, assume no payment method
                has_payment_method = False

        context["has_payment_method"] = has_payment_method
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

        credit_amount = credit_amount_for_purchase_amount_dollars(purchase_amount)

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


class CreateBotView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        try:
            project = get_project_for_user(user=request.user, project_object_id=object_id)

            data = {
                "meeting_url": request.POST.get("meeting_url"),
                "bot_name": request.POST.get("bot_name") or "Meeting Bot",
            }

            bot, error = create_bot(data=data, source=BotCreationSource.DASHBOARD, project=project)
            if error:
                return HttpResponse(json.dumps(error), status=400)

            # If this is a scheduled bot, we don't want to launch it yet.
            if bot.state == BotStates.JOINING:
                launch_bot(bot)

            return HttpResponse("ok", status=200)
        except Exception as e:
            return HttpResponse(str(e), status=400)


class CreateProjectView(AdminRequiredMixin, View):
    def post(self, request):
        name = request.POST.get("name")

        if not name:
            return HttpResponse("Project name is required", status=400)

        if len(name) > 100:
            return HttpResponse("Project name must be less than 100 characters", status=400)

        # Create a new project for the user's organization
        project = Project.objects.create(name=name, organization=request.user.organization)

        # Redirect to the new project's dashboard
        return redirect("bots:project-dashboard", object_id=project.object_id)


class EditProjectView(AdminRequiredMixin, View):
    def put(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        # Parse the request body properly for PUT requests
        put_data = QueryDict(request.body)
        name = put_data.get("name")

        if not name:
            return HttpResponse("Project name is required", status=400)

        if len(name) > 100:
            return HttpResponse("Project name must be less than 100 characters", status=400)

        # Update the project name
        project.name = name
        project.save()

        return HttpResponse("ok", status=200)


class ProjectAutopayStripePortalView(AdminRequiredMixin, View):
    def post(self, request, object_id):
        """Create or update Stripe customer and redirect to billing portal for payment method setup."""
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        organization = project.organization

        try:
            # Check if organization already has a Stripe customer
            if not organization.autopay_stripe_customer_id:
                # Create a new Stripe customer
                customer = stripe.Customer.create(
                    email=request.user.email,
                    name=organization.name,
                    metadata={
                        "organization_id": str(organization.id),
                        "user_id": str(request.user.id),
                    },
                    api_key=os.getenv("STRIPE_SECRET_KEY"),
                )

                # Save the customer ID to the organization
                organization.autopay_stripe_customer_id = customer.id
                organization.save()

            # Check if customer already has a default payment method
            customer = stripe.Customer.retrieve(
                organization.autopay_stripe_customer_id,
                api_key=os.getenv("STRIPE_SECRET_KEY"),
            )
            has_default_payment_method = customer.invoice_settings.default_payment_method is not None

            # Create billing portal session with conditional flow_data
            session_params = {
                "customer": organization.autopay_stripe_customer_id,
                "return_url": request.build_absolute_uri(reverse("projects:project-billing", args=[project.object_id])),
                "api_key": os.getenv("STRIPE_SECRET_KEY"),
            }

            # Only add flow_data if customer doesn't have a default payment method
            if not has_default_payment_method:
                session_params["flow_data"] = {"type": "payment_method_update"}

            session = stripe.billing_portal.Session.create(**session_params)

            # Redirect to the billing portal
            return redirect(session.url)

        except stripe.error.StripeError as e:
            error_id = str(uuid.uuid4())
            logger.error(f"Error setting up payment method (error_id={error_id}): {e}")
            return HttpResponse(f"Error setting up payment method. Error ID: {error_id}", status=400)
        except Exception as e:
            error_id = str(uuid.uuid4())
            logger.error(f"An error occurred setting up payment method (error_id={error_id}): {e}")
            return HttpResponse(f"An error occurred. Error ID: {error_id}", status=500)


class ProjectAutopayView(AdminRequiredMixin, View):
    def patch(self, request, object_id):
        """Update autopay settings for the organization."""
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        organization = project.organization

        try:
            # Parse JSON body
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponse("Invalid JSON", status=400)

        # Validate and update autopay_enabled
        if "autopay_enabled" in data:
            autopay_enabled = data["autopay_enabled"]
            if not isinstance(autopay_enabled, bool):
                return HttpResponse("autopay_enabled must be a boolean", status=400)
            organization.autopay_enabled = autopay_enabled

        # Validate and update autopay_threshold_centricredits
        if "autopay_threshold_credits" in data:
            threshold_credits = data["autopay_threshold_credits"]
            if not isinstance(threshold_credits, (int, float)) or threshold_credits <= 0:
                return HttpResponse("Credit threshold must be a positive number", status=400)
            if threshold_credits > 10000:
                return HttpResponse("Credit threshold cannot exceed 10,000 credits", status=400)
            # Convert credits to centicredits
            organization.autopay_threshold_centricredits = int(threshold_credits * 100)

        # Validate and update autopay_amount_to_purchase_cents
        if "autopay_amount_dollars" in data:
            amount_dollars = data["autopay_amount_dollars"]
            if not isinstance(amount_dollars, (int, float)) or amount_dollars <= 0:
                return HttpResponse("Purchase amount must be a positive number", status=400)
            if amount_dollars < 10:
                return HttpResponse("Purchase amount must be at least $10", status=400)
            if amount_dollars > 10000:
                return HttpResponse("Purchase amount cannot exceed $10,000", status=400)
            # Convert dollars to cents
            organization.autopay_amount_to_purchase_cents = int(amount_dollars * 100)

        try:
            organization.save()
            return HttpResponse("Autopay settings updated successfully", status=200)
        except Exception as e:
            logger.error(f"Error saving autopay settings: {e}")
            return HttpResponse("Error saving autopay settings", status=500)


class CreateGoogleMeetBotLoginView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        try:
            # Get or create GoogleMeetBotLoginGroup for this project
            google_meet_bot_login_group, created = GoogleMeetBotLoginGroup.objects.get_or_create(project=project)

            # Extract fields from request
            workspace_domain = request.POST.get("workspace_domain", "").strip()
            email = request.POST.get("email", "").strip()
            private_key = request.POST.get("private_key", "").strip()
            cert = request.POST.get("cert", "").strip()

            # Validate required fields
            if not all([workspace_domain, email, private_key, cert]):
                return HttpResponse("Missing required fields: workspace_domain, email, private_key, and cert are all required", status=400)

            # Create the GoogleMeetBotLogin
            google_meet_bot_login = GoogleMeetBotLogin.objects.create(
                group=google_meet_bot_login_group,
                workspace_domain=workspace_domain,
                email=email,
            )

            # Set the encrypted credentials
            credentials_data = {
                "private_key": private_key,
                "cert": cert,
            }
            google_meet_bot_login.set_credentials(credentials_data)

            context = self.get_project_context(object_id, project)
            context["google_meet_bot_login_group"] = google_meet_bot_login_group
            return render(request, "projects/partials/google_meet_bot_login_group.html", context)

        except Exception as e:
            error_id = str(uuid.uuid4())
            logger.error(f"Error creating Google Meet bot login (error_id={error_id}): {e}")
            return HttpResponse(f"Error creating Google Meet bot login. Error ID: {error_id}", status=400)


class DeleteGoogleMeetBotLoginView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id, login_object_id):
        google_meet_bot_login = get_google_meet_bot_login_for_user(user=request.user, google_meet_bot_login_object_id=login_object_id)
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        google_meet_bot_login_group = google_meet_bot_login.group
        google_meet_bot_login.delete()
        context = self.get_project_context(object_id, project)
        context["google_meet_bot_login_group"] = google_meet_bot_login_group
        return render(request, "projects/partials/google_meet_bot_login_group.html", context)
