from django.urls import path

from notifications import views

app_name = "notifications"

urlpatterns = [
    path("notifications/", views.NotificationListCreateView.as_view(), name="notification-list-create"),
    path("notifications/unread-count/", views.NotificationUnreadCountView.as_view(), name="notification-unread-count"),
    path("notifications/read-all/", views.NotificationMarkAllReadView.as_view(), name="notification-mark-all-read"),
    path("notifications/fcm-token/", views.FCMTokenView.as_view(), name="fcm-token"),
    path("notifications/<uuid:notification_id>/", views.NotificationDetailView.as_view(), name="notification-detail"),
    path("notifications/<uuid:notification_id>/read/", views.NotificationMarkReadView.as_view(), name="notification-mark-read"),
    path("notification-categories/", views.NotificationCategoryListView.as_view(), name="notification-categories"),
    path("users/me/notification-preferences/", views.UserNotificationPreferenceView.as_view(), name="user-notification-preferences"),
    path("teams/<int:team_id>/notification-preferences/", views.TeamNotificationPreferenceView.as_view(), name="team-notification-preferences"),
]
