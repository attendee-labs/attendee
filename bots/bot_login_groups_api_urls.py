from django.urls import path

from . import bot_login_groups_api_views

urlpatterns = [
    path("bot_login_groups", bot_login_groups_api_views.BotLoginGroupListCreateView.as_view(), name="bot-login-group-list-create"),
    path("bot_login_groups/<str:object_id>", bot_login_groups_api_views.BotLoginGroupDetailDeleteView.as_view(), name="bot-login-group-detail-delete"),
]
