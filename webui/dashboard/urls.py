from __future__ import annotations

from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.dashboard, name="index"),
    path("browse/", views.browse_disks, name="browse"),
    path("launch/", views.launch_job, name="launch"),
    path("configs/", views.config_profile_editor, name="config_profiles"),
    path("configs/save/", views.save_profile, name="save_profile"),
    path("jobs/<int:job_id>/", views.job_detail, name="job_detail"),
    path("jobs/<int:job_id>/run/", views.run_pending_job, name="run_pending_job"),
]
