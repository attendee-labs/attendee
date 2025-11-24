from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class ProjectRateThrottle(SimpleRateThrottle):
    """
    Throttle by project (from request.auth.project)
    """

    scope = "project"

    def get_cache_key(self, request, view):
        auth = getattr(request, "auth", None)
        project = getattr(auth, "project", None)
        if not project:
            return None

        ident = getattr(project, "object_id", "unknown")
        return self.cache_format % {"scope": self.scope, "ident": ident}


class ProjectPostThrottle(ProjectRateThrottle):
    scope = "project_post"

    def allow_request(self, request, view):
        if request.method != "POST":
            return True
        if settings.DISABLE_RATE_LIMITING:
            return True
        return super().allow_request(request, view)
