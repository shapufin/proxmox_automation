from __future__ import annotations

from django.contrib import admin

from .models import MigrationJob


@admin.register(MigrationJob)
class MigrationJobAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "mode", "status", "vm_name", "created_at", "updated_at")
    list_filter = ("mode", "status")
    search_fields = ("name", "vm_name", "manifest_path")
