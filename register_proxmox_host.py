#!/usr/bin/env python3
"""
Script to pre-register the Proxmox host with correct credentials
"""

import os
import sys
import django

# Add the project directory to Python path
sys.path.insert(0, os.path.dirname(__file__))

# Set up Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'webui.settings_production')
django.setup()

from webui.dashboard.models import ProxmoxHost

def register_proxmox_host():
    """Register the Proxmox host with the provided credentials"""
    
    # Check if host already exists
    existing_host = ProxmoxHost.objects.filter(label="Lab Proxmox").first()
    if existing_host:
        print(f"⚠️  Host '{existing_host.label}' already exists. Updating...")
        host = existing_host
    else:
        print("📝 Creating new Proxmox host...")
        host = ProxmoxHost()
        host.label = "Lab Proxmox"
    
    # Set all the credentials
    host.node = "proxmox-node-1"  # You may need to adjust this
    host.api_host = "10.10.10.1"
    host.api_user = "root@pam"
    host.api_token_name = "migration"
    host.api_token_value = "306412a7-6cbc-438e-b382-a22073622ff5"
    host.api_verify_ssl = False
    
    # SSH settings
    host.ssh_enabled = True
    host.ssh_host = "10.10.10.1"
    host.ssh_port = 22
    host.ssh_username = "root"
    host.ssh_password = "Shapufin@1994"
    host.ssh_private_key = ""
    
    # Default settings
    host.default_storage = "local-lvm"
    host.default_bridge = "vmbr0"
    host.notes = "Lab Proxmox host - pre-registered with correct credentials"
    
    # Save the host
    try:
        host.save()
        print(f"✅ Successfully registered/updated Proxmox host: {host.label}")
        print(f"   ID: {host.id}")
        print(f"   API Host: {host.api_host}")
        print(f"   SSH Host: {host.ssh_host}")
        print(f"   Node: {host.node}")
        print(f"   SSH Enabled: {host.ssh_enabled}")
        print("\n🎯 You can now test this host in the UI at /dashboard/hosts/")
        print("   Click the '🔌 Test' button to verify the connection.")
        
    except Exception as e:
        print(f"❌ Failed to save host: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    register_proxmox_host()
