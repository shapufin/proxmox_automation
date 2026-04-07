from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from webui.dashboard.models import JobStatus, MigrationJob
from webui.dashboard.services import execute_job


class Command(BaseCommand):
    help = "Process queued migration jobs"

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Worker started"))
        while True:
            job = self._claim_job()
            if job is None:
                time.sleep(3)
                continue
            self.stdout.write(f"Running job {job.id}: {job.name}")
            try:
                execute_job(job)
            except Exception as exc:  # noqa: BLE001
                job.status = JobStatus.FAILED
                job.error = str(exc)
                job.finished_at = timezone.now()
                job.save(update_fields=["status", "error", "finished_at", "updated_at"])
                self.stderr.write(self.style.ERROR(f"Job {job.id} failed: {exc}"))

    def _claim_job(self):
        with transaction.atomic():
            job = MigrationJob.objects.filter(status=JobStatus.PENDING).order_by("created_at").first()
            if job is None:
                return None
            job.status = JobStatus.RUNNING
            job.started_at = timezone.now()
            job.save(update_fields=["status", "started_at", "updated_at"])
            return job
