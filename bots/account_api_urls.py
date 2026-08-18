from django.urls import path

from . import account_api_views

urlpatterns = [
    path("account", account_api_views.AccountView.as_view(), name="account"),
]
