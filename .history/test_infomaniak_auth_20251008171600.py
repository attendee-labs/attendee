#!/usr/bin/env python
"""
Test different Swift authentication methods for Infomaniak
"""
import os
from swiftclient import client
from dotenv import load_dotenv

load_dotenv()

def test_infomaniak_auth_variations():
    """Test different authentication parameter combinations for Infomaniak"""
    print("Testing Infomaniak Swift Authentication Variations")
    print("=" * 60)
    
    auth_url = os.getenv("OS_AUTH_URL")
    username = os.getenv("OS_USERNAME")
    password = os.getenv("OS_PASSWORD")
    project_name = os.getenv("OS_PROJECT_NAME")
    region = os.getenv("OS_REGION_NAME")
    
    print(f"Using credentials:")
    print(f"  Auth URL: {auth_url}")
    print(f"  Username: {username}")
    print(f"  Project: {project_name}")
    print(f"  Region: {region}")
    print()
    
    # Method 1: Basic with default domains
    print("Method 1: Basic with default domains")
    try:
        conn = client.Connection(
            authurl=auth_url,
            auth_version="3",
            user=username,
            key=password,
            tenant_name=project_name,
            os_options={
                "region_name": region,
                "interface": "public",
                "user_domain_name": "default",
                "project_domain_name": "default",
            },
        )
        auth_result = conn.get_auth()
        print(f"✓ SUCCESS: {auth_result[0][:50]}...")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
    
    # Method 2: Without explicit domain names
    print("\nMethod 2: Without explicit domain names")
    try:
        conn = client.Connection(
            authurl=auth_url,
            auth_version="3",
            user=username,
            key=password,
            tenant_name=project_name,
            os_options={
                "region_name": region,
                "interface": "public",
            },
        )
        auth_result = conn.get_auth()
        print(f"✓ SUCCESS: {auth_result[0][:50]}...")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
    
    # Method 3: Use project_name instead of tenant_name
    print("\nMethod 3: Use project_name instead of tenant_name")
    try:
        conn = client.Connection(
            authurl=auth_url,
            auth_version="3",
            user=username,
            key=password,
            os_options={
                "project_name": project_name,
                "region_name": region,
                "interface": "public",
                "user_domain_name": "default",
                "project_domain_name": "default",
            },
        )
        auth_result = conn.get_auth()
        print(f"✓ SUCCESS: {auth_result[0][:50]}...")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
    
    # Method 4: Try with 'Default' instead of 'default'
    print("\nMethod 4: Try with 'Default' instead of 'default'")
    try:
        conn = client.Connection(
            authurl=auth_url,
            auth_version="3",
            user=username,
            key=password,
            tenant_name=project_name,
            os_options={
                "region_name": region,
                "interface": "public",
                "user_domain_name": "Default",
                "project_domain_name": "Default",
            },
        )
        auth_result = conn.get_auth()
        print(f"✓ SUCCESS: {auth_result[0][:50]}...")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
    
    # Method 5: Try minimal parameters
    print("\nMethod 5: Minimal parameters")
    try:
        conn = client.Connection(
            authurl=auth_url,
            auth_version="3",
            user=username,
            key=password,
            tenant_name=project_name,
        )
        auth_result = conn.get_auth()
        print(f"✓ SUCCESS: {auth_result[0][:50]}...")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
    
    return False

if __name__ == "__main__":
    success = test_infomaniak_auth_variations()
    if success:
        print("\n🎉 Found working authentication method!")
    else:
        print("\n❌ No authentication method worked.")
        print("Please verify your Infomaniak credentials in the web console.")