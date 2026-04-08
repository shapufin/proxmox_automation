#!/usr/bin/env python3
"""
Test script to validate Proxmox connection before registering in the UI
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from vmware_to_proxmox.proxmox import ProxmoxClient

def test_proxmox_connection():
    """Test both API and SSH connections to Proxmox"""
    print("🔍 Testing Proxmox connection...")
    
    # Test API connection
    print("\n📡 Testing API connection...")
    api_client = ProxmoxClient(
        node="proxmox-node-1",
        api_host="10.10.10.1",
        api_user="root@pam",
        api_token_name="migration",
        api_token_value="306412a7-6cbc-438e-b382-a22073622ff5",
        api_verify_ssl=False
    )
    
    try:
        api = api_client._api_client()
        if api:
            print("✅ API connection successful!")
            version = api.version.get()
            print(f"   Proxmox version: {version.get('version', 'unknown')}")
        else:
            print("❌ API connection failed")
    except Exception as e:
        print(f"❌ API connection error: {e}")
    
    # Test SSH connection
    print("\n🔐 Testing SSH connection...")
    ssh_client = ProxmoxClient(
        node="proxmox-node-1",
        ssh_enabled=True,
        ssh_host="10.10.10.1",
        ssh_port=22,
        ssh_username="root",
        ssh_password="Shapufin@1994"
    )
    
    try:
        ssh_client.ensure_prerequisites()
        print("✅ SSH prerequisites check passed!")
        
        # Test a simple command
        result = ssh_client._run(["hostname"], check=False)
        if result.returncode == 0:
            print(f"✅ SSH command successful! Hostname: {result.stdout.strip()}")
        else:
            print(f"❌ SSH command failed: {result.stderr}")
    except Exception as e:
        print(f"❌ SSH connection error: {e}")
    
    # Test combined connection (like the UI would use)
    print("\n🔄 Testing combined connection (like UI)...")
    combined_client = ProxmoxClient(
        node="proxmox-node-1",
        ssh_enabled=True,
        ssh_host="10.10.10.1",
        ssh_port=22,
        ssh_username="root",
        ssh_password="Shapufin@1994",
        api_host="10.10.10.1",
        api_user="root@pam",
        api_token_name="migration",
        api_token_value="306412a7-6cbc-438e-b382-a22073622ff5",
        api_verify_ssl=False
    )
    
    try:
        # Test prerequisites
        combined_client.ensure_prerequisites()
        print("✅ Combined prerequisites check passed!")
        
        # Test API
        api = combined_client._api_client()
        if api:
            print("✅ Combined API connection successful!")
        
        # Test bridges discovery
        bridges = combined_client.list_bridges()
        print(f"✅ Found {len(bridges)} bridges/networks:")
        for bridge in bridges[:5]:  # Show first 5
            print(f"   - {bridge.name} (active: {bridge.active})")
        
        # Test storage discovery
        storages = combined_client.list_storages()
        print(f"✅ Found {len(storages)} storages:")
        for storage in storages[:5]:  # Show first 5
            print(f"   - {storage.storage} ({storage.storage_type})")
            
        print("\n🎉 All tests passed! Your Proxmox connection is working correctly.")
        
    except Exception as e:
        print(f"❌ Combined connection error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_proxmox_connection()
