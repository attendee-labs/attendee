from django.urls import path

from . import bots_api_views

urlpatterns = [
    path("bots", bots_api_views.BotListCreateView.as_view(), name="bot-list-create"),
    path(
        "bots/<str:object_id>",
        bots_api_views.BotDetailView.as_view(),
        name="bot-detail",
    ),
    path(
        "bots/<str:object_id>/leave",
        bots_api_views.BotLeaveView.as_view(),
        name="bot-leave",
    ),
    path(
        "bots/<str:object_id>/transcript",
        bots_api_views.TranscriptView.as_view(),
        name="bot-transcript",
    ),
    path(
        "bots/<str:object_id>/recording",
        bots_api_views.RecordingView.as_view(),
        name="bot-recording",
    ),
    path(
        "bots/<str:object_id>/output_audio",
        bots_api_views.OutputAudioView.as_view(),
        name="bot-output-audio",
    ),
    path(
        "bots/<str:object_id>/output_image",
        bots_api_views.OutputImageView.as_view(),
        name="bot-output-image",
    ),
    path(
        "bots/<str:object_id>/output_video",
        bots_api_views.OutputVideoView.as_view(),
        name="bot-output-video",
    ),
    path(
        "bots/<str:object_id>/speech",
        bots_api_views.SpeechView.as_view(),
        name="bot-speech",
    ),
    path(
        "bots/<str:object_id>/chat_messages",
        bots_api_views.ChatMessagesView.as_view(),
        name="bot-chat-messages",
    ),
    path(
        "bots/<str:object_id>/send_chat_message",
        bots_api_views.SendChatMessageView.as_view(),
        name="bot-send-chat-message",
    ),
    path(
        "bots/<str:object_id>/delete_data",
        bots_api_views.DeleteDataView.as_view(),
        name="bot-delete-data",
    ),
    path(
        "bots/<str:object_id>/pause_recording",
        bots_api_views.PauseRecordingView.as_view(),
        name="bot-pause-recording",
    ),
    path(
        "bots/<str:object_id>/resume_recording",
        bots_api_views.ResumeRecordingView.as_view(),
        name="bot-resume-recording",
    ),
    path(
        "bots/<str:object_id>/admit_from_waiting_room",
        bots_api_views.AdmitFromWaitingRoomView.as_view(),
        name="bot-admit-from-waiting-room",
    ),
    path(
        "bots/<str:object_id>/participant_events",
        bots_api_views.ParticipantEventsView.as_view(),
        name="bot-participant-events",
    ),
    path(
        "bots/<str:object_id>/participants",
        bots_api_views.ParticipantsView.as_view(),
        name="bot-participants",
    ),
]

# catch any other paths and return a 404 json response - must be last
urlpatterns += [path("<path:any>", bots_api_views.NotFoundView.as_view())]
