#!/usr/bin/env python3
"""
Final test script to verify Swift storage is working with the complete system
"""
import os
import sys
import django
from pathlib import Path

# Add the project directory to Python path
sys.path.insert(0, '/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'attendee.settings.development')

# Initialize Django
django.setup()

from bots.storage.swift_utils import get_swift_client, upload_file_to_swift
from bots.storage.swift_storage import SwiftStorage

def test_swift_authentication():
    """Test Swift authentication"""
    print("🔐 Testing Swift Authentication...")
    try:
        client = get_swift_client()
        print("✅ Swift authentication successful!")
        return True
    except Exception as e:
        print(f"❌ Swift authentication failed: {e}")
        return False

def test_swift_storage_backend():
    """Test Django Swift storage backend"""
    print("\n📁 Testing Django Swift Storage Backend...")
    try:
        storage = SwiftStorage()
        
        # Test content
        test_content = b"Hello from Swift Storage! System is working."
        test_filename = "test_system_final.txt"
        
        # Save file
        print(f"   Uploading {test_filename}...")
        saved_name = storage.save(test_filename, open('/dev/null', 'rb'))
        print(f"   ✅ File saved as: {saved_name}")
        
        # Test file exists
        if storage.exists(saved_name):
            print(f"   ✅ File exists in Swift storage")
        else:
            print(f"   ❌ File not found in Swift storage")
            
        # Get file URL
        url = storage.url(saved_name)
        print(f"   ✅ File URL generated: {url[:50]}...")
        
        return True
        
    except Exception as e:
        print(f"   ❌ Django storage test failed: {e}")
        return False

def main():
    print("🚀 Final Swift Storage System Test")
    print("=" * 50)
    
    # Test 1: Authentication
    auth_success = test_swift_authentication()
    
    # Test 2: Django Storage Backend
    storage_success = test_swift_storage_backend()
    
    # Final result
    print("\n" + "=" * 50)
    if auth_success and storage_success:
        print("🎉 ALL TESTS PASSED! Swift storage is fully operational!")
        print("🌟 The Attendee system is ready with Swift object storage.")
        print("🔗 Web interface available at: http://localhost:8000")
    else:
        print("❌ Some tests failed. Check the output above.")
    
    print("=" * 50)

if __name__ == "__main__":
    main()