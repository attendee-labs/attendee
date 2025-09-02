from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect

from bots.models import Project


@login_required
def home(request):
    # Get the first bot for the user
    project = Project.accessible_to(request.user).first()
    if not project:
        project = Project.objects.create(
            name=f"{request.user.email}'s project",
            organization=request.user.organization,
        )
    if project:
        return redirect("projects:project-dashboard", object_id=project.object_id)
    raise Http404("No projects found for this organization. You need to create a project first.")
