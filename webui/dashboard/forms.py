from __future__ import annotations

from pathlib import Path

from django import forms

from .models import MigrationMode


class DiskBrowseForm(forms.Form):
    directory = forms.CharField(required=False, label="Directory")


class ConfigProfileForm(forms.Form):
    name = forms.CharField(max_length=255)
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 24, "spellcheck": "false"}))


class MigrationJobForm(forms.Form):
    name = forms.CharField(max_length=255)
    mode = forms.ChoiceField(choices=MigrationMode.choices)
    directory = forms.CharField(max_length=1024, required=False, initial="")
    config_profile = forms.ChoiceField(choices=(), required=False)
    vm_name = forms.ChoiceField(choices=(), required=False)
    manifest_path = forms.CharField(max_length=1024, required=False)
    storage = forms.ChoiceField(choices=(), required=False)
    bridge = forms.ChoiceField(choices=(), required=False)
    disk_format = forms.ChoiceField(choices=[("", "Auto"), ("qcow2", "qcow2"), ("raw", "raw")], required=False)
    dry_run = forms.BooleanField(required=False, initial=True)
    start_after_import = forms.BooleanField(required=False, initial=True)
    source_paths = forms.MultipleChoiceField(choices=(), required=False, widget=forms.CheckboxSelectMultiple)

    def set_source_choices(self, choices: list[tuple[str, str]]) -> None:
        self.fields["source_paths"].choices = choices

    def set_config_profile_choices(self, choices: list[tuple[str, str]]) -> None:
        self.fields["config_profile"].choices = [("", "Default config")] + choices

    def set_vm_choices(self, choices: list[tuple[str, str]]) -> None:
        self.fields["vm_name"].choices = [("", "Select a VMware VM")] + choices

    def set_storage_choices(self, choices: list[tuple[str, str]]) -> None:
        self.fields["storage"].choices = [("", "Auto-select storage")] + choices

    def set_bridge_choices(self, choices: list[tuple[str, str]]) -> None:
        self.fields["bridge"].choices = [("", "Auto-select bridge")] + choices

    def normalized_source_paths(self) -> list[str]:
        values = self.cleaned_data.get("source_paths", [])
        if isinstance(values, str):
            values = [values]
        return [str(Path(item)) for item in values]

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("mode")
        vm_name = (cleaned.get("vm_name") or "").strip()
        manifest_path = (cleaned.get("manifest_path") or "").strip()
        source_paths = cleaned.get("source_paths") or []

        if mode == MigrationMode.LOCAL:
            if not manifest_path:
                self.add_error("manifest_path", "A manifest path is required for local disk imports.")
            if not source_paths:
                self.add_error("source_paths", "Select at least one local disk file.")
        else:
            if not vm_name:
                self.add_error("vm_name", "A VMware VM name is required for direct migrations.")

        return cleaned
