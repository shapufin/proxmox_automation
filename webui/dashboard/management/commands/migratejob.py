from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from webui.dashboard.models import JobStatus, MigrationJob
from webui.dashboard.services import execute_job


class Command(BaseCommand):
    help = "Run a single queued migration job by ID"

    def add_arguments(self, parser):
        parser.add_argument("job_id", type=int)

    def handle(self, *args, **options):
        job = MigrationJob.objects.get(pk=options["job_id"])
        job.status = JobStatus.RUNNING
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at", "updated_at"])
        execute_job(job)
        self.stdout.write(self.style.SUCCESS(f"Job {job.id} completed"))
