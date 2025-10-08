#!/usr/bin/env python
"""
Minimal Swift authentication test to isolate the issue
"""
import os
from swiftclient import client

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()

def test_auth_methods():
    """Test different authentication methods"""
    print("Swift Authentication Debug Test")
    print("=" * 40)
    
    # Print current environment variables
    print("Environment Variables:")
    for var in ["OS_AUTH_URL", "OS_APPLICATION_CREDENTIAL_ID", "OS_APPLICATION_CREDENTIAL_SECRET", 
                "OS_USERNAME", "OS_PASSWORD", "OS_PROJECT_NAME", "OS_REGION_NAME", "OS_INTERFACE"]:
        value = os.getenv(var)
        if value:
            if "SECRET" in var or "PASSWORD" in var:
                print(f"  {var}: {value[:10]}... (length: {len(value)})")
            else:
                print(f"  {var}: {value}")
        else:
            print(f"  {var}: NOT SET")
    print()
    
    # Test method 1: Exact working implementation format
    print("Test 1: Exact working implementation format")
    try:
        conn = client.Connection(
            authurl=os.getenv("OS_AUTH_URL"),
            auth_version="3",
            os_options={
                "auth_type": "v3applicationcredential",
                "application_credential_id": os.getenv("OS_APPLICATION_CREDENTIAL_ID"),
                "application_credential_secret": os.getenv("OS_APPLICATION_CREDENTIAL_SECRET"),
                "region_name": os.getenv("OS_REGION_NAME"),
                "interface": os.getenv("OS_INTERFACE", "public"),
            },
        )
        auth_url, token = conn.get_auth()
        print(f"✓ SUCCESS: Token length {len(token)}")
        print(f"  Auth URL: {auth_url}")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
    
    # Test method 2: Try without explicit auth_type
    print("\nTest 2: Without explicit auth_type")
    try:
        conn = client.Connection(
            authurl=os.getenv("OS_AUTH_URL"),
            auth_version="3",
            os_options={
                "application_credential_id": os.getenv("OS_APPLICATION_CREDENTIAL_ID"),
                "application_credential_secret": os.getenv("OS_APPLICATION_CREDENTIAL_SECRET"),
                "region_name": os.getenv("OS_REGION_NAME"),
                "interface": os.getenv("OS_INTERFACE", "public"),
            },
        )
        auth_url, token = conn.get_auth()
        print(f"✓ SUCCESS: Token length {len(token)}")
        return True
    except Exception as e:
        print(f"✗ FAILED: {e}")
    
    # Test method 3: Try with username/password if available
    username = os.getenv("SWIFT_USERNAME")
    password = os.getenv("SWIFT_PASSWORD") 
    tenant = os.getenv("SWIFT_TENANT_NAME")
    
    if username and password and tenant:
        print("\nTest 3: Username/password authentication")
        try:
            conn = client.Connection(
                authurl=os.getenv("OS_AUTH_URL"),
                user=username,
                key=password,
                tenant_name=tenant,
                auth_version="3",
                os_options={
                    "region_name": os.getenv("OS_REGION_NAME"),
                    "interface": os.getenv("OS_INTERFACE", "public"),
                }
            )
            auth_url, token = conn.get_auth()
            print(f"✓ SUCCESS: Token length {len(token)}")
            return True
        except Exception as e:
            print(f"✗ FAILED: {e}")
    
    return False

if __name__ == "__main__":
    success = test_auth_methods()
    if not success:
        print("\n❌ All authentication methods failed.")
        print("This suggests the credentials may be invalid, expired, or there's a configuration issue.")
        print("Please check your Infomaniak cloud console to verify the application credentials.")
    else:
        print("\n✅ Authentication successful!")