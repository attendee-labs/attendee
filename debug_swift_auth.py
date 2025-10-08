#!/usr/bin/env python3
"""
Direct test of Swift credentials to debug authentication issues
"""
import os
from swiftclient import client as swift_client
from swiftclient.exceptions import ClientException

def test_swift_credentials():
    print("Testing Swift Credentials Directly")
    print("=" * 50)
    
    auth_url = "https://api.pub1.infomaniak.cloud/identity/v3"
    username = "PCU-UNEYEHZ"
    password = "m]}:Pz<~p6!rx3sWws"
    tenant_name = "PCP-WRUWDUC"
    
    print(f"Auth URL: {auth_url}")
    print(f"Username: {username}")
    print(f"Tenant: {tenant_name}")
    print()
    
    # Test 1: Basic connection
    print("Test 1: Basic Swift connection")
    try:
        conn = swift_client.Connection(
            authurl=auth_url,
            user=username,
            key=password,
            tenant_name=tenant_name,
            auth_version='3',
            retries=1
        )
        
        # Try to get account info
        account_info = conn.head_account()
        print("✅ Basic connection successful!")
        print(f"Account info: {account_info}")
        
    except Exception as e:
        print(f"❌ Basic connection failed: {e}")
        print()
    
    # Test 2: With domain specifications
    print("Test 2: With explicit domain settings")
    try:
        conn = swift_client.Connection(
            authurl=auth_url,
            user=username,
            key=password,
            tenant_name=tenant_name,
            auth_version='3',
            retries=1,
            os_options={
                'project_name': tenant_name,
                'user_domain_name': 'default',
                'project_domain_name': 'default',
            }
        )
        
        account_info = conn.head_account()
        print("✅ Domain-specific connection successful!")
        print(f"Account info: {account_info}")
        
    except Exception as e:
        print(f"❌ Domain-specific connection failed: {e}")
        print()
    
    # Test 3: List containers if successful
    print("Test 3: Try to list containers")
    try:
        conn = swift_client.Connection(
            authurl=auth_url,
            user=username,
            key=password,
            tenant_name=tenant_name,
            auth_version='3',
            retries=1
        )
        
        headers, containers = conn.get_account()
        print("✅ Container listing successful!")
        print(f"Found {len(containers)} containers:")
        for container in containers:
            print(f"  - {container['name']} ({container['count']} objects, {container['bytes']} bytes)")
            
    except Exception as e:
        print(f"❌ Container listing failed: {e}")
        print()
    
    # Test 4: Check if specific container exists
    print("Test 4: Check specific container 'transcript-meets'")
    try:
        conn = swift_client.Connection(
            authurl=auth_url,
            user=username,
            key=password,
            tenant_name=tenant_name,
            auth_version='3',
            retries=1
        )
        
        container_info = conn.head_container('transcript-meets')
        print("✅ Container 'transcript-meets' exists!")
        print(f"Container info: {container_info}")
        
    except ClientException as e:
        if e.http_status == 404:
            print("⚠️  Container 'transcript-meets' does not exist (will be created automatically)")
        else:
            print(f"❌ Container check failed: {e}")
    except Exception as e:
        print(f"❌ Container check failed: {e}")

if __name__ == "__main__":
    test_swift_credentials()