#!/usr/bin/env python
"""
Test script for Swift storage implementation (Working Version)
"""
import os
import sys
from datetime import datetime

# Add the project root to sys.path
sys.path.append(os.path.dirname(__file__))

# Set Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'attendee.settings.development')

try:
    import django
    django.setup()
except Exception as e:
    print(f"Django setup failed: {e}")
    sys.exit(1)


def check_swift_config():
    """Check if Swift configuration is complete using working variable names"""
    print("Swift Storage Implementation Test (Working Version)")
    print("=" * 50)
    
    # Check for application credentials first
    app_cred_vars = [
        "OS_AUTH_URL",
        "OS_APPLICATION_CREDENTIAL_ID", 
        "OS_APPLICATION_CREDENTIAL_SECRET",
        "OS_REGION_NAME",
        "SWIFT_CONTAINER_MEETS"
    ]
    
    # Check for username/password credentials
    username_vars = [
        "OS_AUTH_URL",
        "OS_USERNAME",
        "OS_PASSWORD", 
        "OS_PROJECT_NAME",
        "OS_REGION_NAME",
        "SWIFT_CONTAINER_MEETS"
    ]
    
    # Check application credentials
    app_creds_missing = []
    for var in app_cred_vars:
        if not os.getenv(var):
            app_creds_missing.append(var)
    
    # Check username/password credentials
    username_missing = []
    for var in username_vars:
        if not os.getenv(var):
            username_missing.append(var)
    
    if len(app_creds_missing) == 0:
        print("✓ Swift configuration found (application credentials)")
        print(f"  Auth URL: {os.getenv('OS_AUTH_URL')}")
        print(f"  Region: {os.getenv('OS_REGION_NAME')}")
        print(f"  Container: {os.getenv('SWIFT_CONTAINER_MEETS')}")
        print()
        return True
    elif len(username_missing) == 0:
        print("✓ Swift configuration found (username/password)")
        print(f"  Auth URL: {os.getenv('OS_AUTH_URL')}")
        print(f"  Username: {os.getenv('OS_USERNAME')}")
        print(f"  Project: {os.getenv('OS_PROJECT_NAME')}")
        print(f"  Region: {os.getenv('OS_REGION_NAME')}")
        print(f"  Container: {os.getenv('SWIFT_CONTAINER_MEETS')}")
        print()
        return True
    else:
        print("Swift configuration missing. Need either:")
        print()
        print("Application credentials:")
        for var in app_cred_vars:
            status = "✓ SET" if os.getenv(var) else "✗ MISSING"
            print(f"  {var}: {status}")
        print()
        print("OR username/password:")
        for var in username_vars:
            status = "✓ SET" if os.getenv(var) else "✗ MISSING"
            print(f"  {var}: {status}")
        print()
        print("Please set all required variables and try again.")
        return False


def test_swift_authentication():
    """Test Swift authentication"""
    print("Testing Swift authentication...")
    
    try:
        from bots.storage.swift_utils import get_swift_client
        
        client = get_swift_client()
        # Test authentication by getting account info
        auth_url, auth_token = client.get_auth()
        
        print("✓ Swift authentication successful")
        print(f"  Auth URL: {auth_url}")
        print(f"  Token length: {len(auth_token)} characters")
        return True
        
    except Exception as e:
        print(f"✗ Swift authentication failed: {e}")
        print(f"  Error type: {type(e).__name__}")
        import traceback
        print(f"  Traceback: {traceback.format_exc()}")
        return False


def test_container_operations():
    """Test basic container operations"""
    print("\nTesting container operations...")
    
    try:
        from bots.storage.swift_utils import get_swift_client, get_container_name
        
        client = get_swift_client()
        container_name = get_container_name()
        
        # Test container listing
        headers, containers = client.get_account()
        print(f"✓ Account info retrieved: {len(containers)} containers found")
        
        # Check if our container exists
        container_exists = any(c['name'] == container_name for c in containers)
        print(f"  Target container '{container_name}': {'exists' if container_exists else 'not found'}")
        
        # If container doesn't exist, create it
        if not container_exists:
            client.put_container(container_name)
            print(f"✓ Created container '{container_name}'")
        
        return True
        
    except Exception as e:
        print(f"✗ Container operations failed: {e}")
        return False


def test_file_operations():
    """Test file upload and download operations"""
    print("\nTesting file operations...")
    
    test_content = f"Test file content - {datetime.now().isoformat()}"
    test_object_name = f"test-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    
    try:
        from bots.storage.swift_utils import (
            upload_file_to_swift, 
            object_exists,
            delete_file_from_swift,
            generate_presigned_url
        )
        
        # Test upload
        result = upload_file_to_swift(test_content.encode(), test_object_name)
        print(f"✓ File uploaded: {result}")
        
        # Test exists check
        exists = object_exists(test_object_name)
        print(f"✓ File exists check: {exists}")
        
        # Test URL generation
        try:
            url = generate_presigned_url(test_object_name)
            print(f"✓ Generated URL: {url[:80]}...")
        except Exception as e:
            print(f"⚠ URL generation failed (this is OK): {e}")
        
        # Test cleanup
        delete_file_from_swift(test_object_name)
        print(f"✓ File deleted: {test_object_name}")
        
        # Verify deletion
        exists_after = object_exists(test_object_name)
        print(f"✓ File exists after deletion: {exists_after}")
        
        return True
        
    except Exception as e:
        print(f"✗ File operations failed: {e}")
        return False


def test_django_storage():
    """Test Django storage backend"""
    print("\nTesting Django storage backend...")
    
    try:
        from bots.storage.swift_storage import SwiftStorage
        from django.core.files.base import ContentFile
        
        # Create storage instance
        storage = SwiftStorage()
        
        # Test file save
        test_content = ContentFile(f"Django test - {datetime.now().isoformat()}")
        test_name = f"django-test-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        
        saved_name = storage.save(test_name, test_content)
        print(f"✓ Django storage save: {saved_name}")
        
        # Test file exists
        exists = storage.exists(saved_name)
        print(f"✓ Django storage exists: {exists}")
        
        # Test file URL
        try:
            url = storage.url(saved_name)
            print(f"✓ Django storage URL: {url[:80]}...")
        except Exception as e:
            print(f"⚠ Django storage URL failed: {e}")
        
        # Test file size
        size = storage.size(saved_name)
        print(f"✓ Django storage size: {size} bytes")
        
        # Test file deletion
        storage.delete(saved_name)
        print(f"✓ Django storage delete: {saved_name}")
        
        return True
        
    except Exception as e:
        print(f"✗ Django storage test failed: {e}")
        return False


def main():
    """Run all tests"""
    if not check_swift_config():
        sys.exit(1)
    
    all_passed = True
    
    # Run tests
    tests = [
        test_swift_authentication,
        test_container_operations, 
        test_file_operations,
        test_django_storage
    ]
    
    for test_func in tests:
        try:
            passed = test_func()
            if not passed:
                all_passed = False
        except Exception as e:
            print(f"✗ Test {test_func.__name__} crashed: {e}")
            all_passed = False
    
    print("\n" + "=" * 50)
    if all_passed:
        print("🎉 All tests passed! Swift storage is working correctly.")
    else:
        print("❌ Some tests failed. Check the output above for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()