from __future__ import annotations

from pathlib import Path

from django import forms

from .models import MigrationMode, ProxmoxHost, VMwareHost


class ProxmoxHostForm(forms.ModelForm):
    class Meta:
        model = ProxmoxHost
        fields = [
            "label", "node", "api_host", "api_user",
            "api_token_name", "api_token_value", "api_verify_ssl",
            "ssh_enabled", "ssh_host", "ssh_port", "ssh_username",
            "ssh_password", "ssh_private_key",
            "default_storage", "default_bridge", "notes",
        ]
        widgets = {
            "api_token_value": forms.PasswordInput(render_value=True, attrs={"autocomplete": "off"}),
            "ssh_password":    forms.PasswordInput(render_value=True, attrs={"autocomplete": "off"}),
            "ssh_private_key": forms.Textarea(attrs={"rows": 4, "placeholder": "Paste PEM private key here (optional)"}),
            "notes":           forms.Textarea(attrs={"rows": 2}),
        }


class VMwareHostForm(forms.ModelForm):
    class Meta:
        model = VMwareHost
        fields = ["label", "host", "username", "password", "port", "allow_insecure_ssl", "notes"]
        widgets = {
            "password": forms.PasswordInput(render_value=True, attrs={"autocomplete": "off"}),
            "notes":    forms.Textarea(attrs={"rows": 2}),
        }


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
    storage = forms.CharField(max_length=255, required=False)
    bridge  = forms.CharField(max_length=255, required=False)
    disk_format = forms.ChoiceField(choices=[("", "Auto"), ("qcow2", "qcow2"), ("raw", "raw")], required=False)
    disk_storage_map = forms.CharField(required=False, initial="{}", widget=forms.HiddenInput)
    nic_bridge_map = forms.CharField(required=False, initial="{}", widget=forms.HiddenInput)
    proxmox_host_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    vmware_host_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    dry_run = forms.BooleanField(required=False, initial=True)
    start_after_import = forms.BooleanField(required=False, initial=True)
    source_paths = forms.MultipleChoiceField(choices=(), required=False, widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Allow any non-empty path value (host-absolute paths from wizard are valid)
        self.fields["source_paths"].valid_value = lambda val: bool(val)

    def set_source_choices(self, choices: list[tuple[str, str]]) -> None:
        self.fields["source_paths"].choices = choices

    def set_config_profile_choices(self, choices: list[tuple[str, str]]) -> None:
        self.fields["config_profile"].choices = [("", "Default config")] + choices

    def set_vm_choices(self, choices: list[tuple[str, str]]) -> None:
        self.fields["vm_name"].choices = [("", "Select a VMware VM")] + choices

    def set_storage_choices(self, choices: list[tuple[str, str]]) -> None:
        pass  # storage is now a free-text CharField

    def set_bridge_choices(self, choices: list[tuple[str, str]]) -> None:
        pass  # bridge is now a free-text CharField

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
            if not source_paths:
                self.add_error("source_paths", "Select at least one local disk file.")
        else:
            if not vm_name:
                self.add_error("vm_name", "A VMware VM name is required for direct migrations.")

        return cleaned
