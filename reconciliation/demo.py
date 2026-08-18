"""
Public demo mode.

The deployed demo is unauthenticated so anyone can open the URL and explore the
seeded data. That means the write endpoints have to be closed off instead --
otherwise any visitor could upload a CSV into the container or rewrite
discrepancy statuses.

Both decorators are no-ops when DEMO_READONLY is off, so the app behaves exactly
as written outside the demo deployment.
"""

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect

DEMO_WRITE_MESSAGE = (
    "This is a read-only public demo, so that action is disabled. "
    "Everything else is live against the seeded data."
)


def demo_aware_login_required(view):
    """login_required, unless the deployment is a public demo."""
    protected = login_required(view)

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if settings.DEMO_READONLY:
            return view(request, *args, **kwargs)
        return protected(request, *args, **kwargs)

    return wrapper


def blocked_in_demo(view):
    """Refuse a state-changing view while in demo mode.

    JSON endpoints get a 403 with a JSON body -- the jQuery front end parses
    every response as JSON, so an HTML redirect here would surface as a console
    error rather than a readable message.
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not settings.DEMO_READONLY:
            return view(request, *args, **kwargs)

        if request.path.startswith("/api/"):
            return JsonResponse({"error": DEMO_WRITE_MESSAGE}, status=403)

        messages.info(request, DEMO_WRITE_MESSAGE)
        return redirect("dashboard")

    return wrapper


def demo_context(request):
    """Expose the demo flag to every template."""
    return {"demo_readonly": settings.DEMO_READONLY}
