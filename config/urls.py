"""Project URL configuration."""

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = []

# The public demo runs unauthenticated, so the admin -- a real login form with
# full write access -- is not routed at all there. No superuser is created
# either, but leaving the URL exposed invites brute-force noise for no benefit.
if not settings.DEMO_READONLY:
    urlpatterns += [
        path("admin/", admin.site.urls),
        path(
            "accounts/login/",
            auth_views.LoginView.as_view(template_name="registration/login.html"),
            name="login",
        ),
        path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    ]

urlpatterns += [
    path("", include("reconciliation.urls")),
]
