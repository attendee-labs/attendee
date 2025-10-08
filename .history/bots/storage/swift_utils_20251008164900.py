"""
Swift object storage utilities using python-swiftclient
"""
import os
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import urljoin
from swiftclient import client as swift_client
from swiftclient.exceptions import ClientException

logger = logging.getLogger(__name__)


def get_swift_client():
    """Create and return a Swift client instance"""
    auth_url = os.getenv("SWIFT_AUTH_URL")
    auth_version = os.getenv("SWIFT_AUTH_VERSION", "3")
    
    # Check if we have application credentials (preferred for Infomaniak)
    app_cred_id = os.getenv("SWIFT_APPLICATION_CREDENTIAL_ID")
    app_cred_secret = os.getenv("SWIFT_APPLICATION_CREDENTIAL_SECRET")
    
    # Check if we have username/password credentials
    username = os.getenv("SWIFT_USERNAME") 
    password = os.getenv("SWIFT_PASSWORD")
    tenant_name = os.getenv("SWIFT_TENANT_NAME")
    
    if not auth_url:
        raise ValueError("SWIFT_AUTH_URL is required")
    
    # Try application credentials first (recommended for Infomaniak)
    if app_cred_id and app_cred_secret:
        logger.info("Using Swift application credentials authentication")
        try:
            return swift_client.Connection(
                authurl=auth_url,
                auth_version=auth_version,
                retries=3,
                os_options={
                    'application_credential_id': app_cred_id,
                    'application_credential_secret': app_cred_secret,
                }
            )
        except Exception as e:
            logger.error(f"Application credentials authentication failed: {e}")
            # Fall back to username/password if available
    
    # Fall back to username/password authentication
    if username and password and tenant_name:
        logger.info("Using Swift username/password authentication")
        try:
            # First try: Standard OpenStack v3 with project name
            return swift_client.Connection(
                authurl=auth_url,
                user=username,
                key=password,
                tenant_name=tenant_name,
                auth_version=auth_version,
                retries=3,
                os_options={
                    'project_name': tenant_name,
                    'user_domain_name': 'Default',
                    'project_domain_name': 'Default',
                }
            )
        except Exception as e1:
            logger.warning(f"First auth attempt failed: {e1}")
            try:
                # Second try: Simplified approach
                return swift_client.Connection(
                    authurl=auth_url,
                    user=username,
                    key=password,
                    tenant_name=tenant_name,
                    auth_version=auth_version,
                    retries=3
                )
            except Exception as e2:
                logger.error(f"Username/password authentication failed: {e2}")
                raise
    
    raise ValueError("Swift credentials not properly configured. Please set either (SWIFT_APPLICATION_CREDENTIAL_ID + SWIFT_APPLICATION_CREDENTIAL_SECRET) or (SWIFT_USERNAME + SWIFT_PASSWORD + SWIFT_TENANT_NAME)")


def get_container_name():
    """Get the Swift container name from environment variables"""
    container_name = os.getenv("SWIFT_CONTAINER_NAME", "recordings")
    return container_name


def upload_file_to_swift(file_content_or_path, object_name):
    """Upload file to Swift storage

    Args:
        file_content_or_path: Either bytes content or path to file
        object_name: Name of the object in Swift storage
    
    Returns:
        str: The object name that was uploaded
    """
    swift = get_swift_client()
    container_name = get_container_name()
    
    try:
        # Ensure container exists
        try:
            swift.head_container(container_name)
        except ClientException as e:
            if e.http_status == 404:
                # Container doesn't exist, create it
                swift.put_container(container_name)
                logger.info(f"Created Swift container: {container_name}")
            else:
                raise
        
        if isinstance(file_content_or_path, (bytes, str)):
            # If it's bytes or string content, upload directly
            if isinstance(file_content_or_path, str) and os.path.exists(file_content_or_path):
                # It's a file path
                with open(file_content_or_path, "rb") as file_obj:
                    swift.put_object(container_name, object_name, contents=file_obj)
            else:
                # It's content (bytes or string)
                swift.put_object(container_name, object_name, contents=file_content_or_path)
        else:
            # Assume it's a file path
            with open(file_content_or_path, "rb") as file_obj:
                swift.put_object(container_name, object_name, contents=file_obj)
        
        logger.info(f"Successfully uploaded {object_name} to Swift container {container_name}")
        return object_name
        
    except Exception as e:
        logger.error(f"Failed to upload {object_name} to Swift: {e}")
        raise


def delete_file_from_swift(object_name):
    """Delete a file from Swift storage
    
    Args:
        object_name: Name of the object to delete
    """
    swift = get_swift_client()
    container_name = get_container_name()
    
    try:
        swift.delete_object(container_name, object_name)
        logger.info(f"Successfully deleted {object_name} from Swift container {container_name}")
    except ClientException as e:
        if e.http_status == 404:
            logger.warning(f"Object {object_name} not found in Swift container {container_name}")
        else:
            logger.error(f"Failed to delete {object_name} from Swift: {e}")
            raise


def object_exists(object_name):
    """Check if an object exists in Swift storage
    
    Args:
        object_name: Name of the object to check
        
    Returns:
        bool: True if object exists, False otherwise
    """
    swift = get_swift_client()
    container_name = get_container_name()
    
    try:
        swift.head_object(container_name, object_name)
        return True
    except ClientException as e:
        if e.http_status == 404:
            return False
        else:
            logger.error(f"Error checking if object {object_name} exists: {e}")
            raise


def list_objects_in_container(prefix: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """List objects in the Swift container
    
    Args:
        prefix: Optional prefix to filter objects
        limit: Optional limit on number of objects to return
        
    Returns:
        List of object dictionaries
    """
    swift = get_swift_client()
    container_name = get_container_name()
    
    try:
        options = {}
        if prefix:
            options['prefix'] = prefix
        if limit:
            options['limit'] = limit
            
        headers, objects = swift.get_container(container_name, **options)
        return objects
    except Exception as e:
        logger.error(f"Failed to list objects in Swift container {container_name}: {e}")
        raise


def generate_presigned_url(object_name, expiry_seconds=3600):
    """Generate a temporary URL for accessing a Swift object
    
    Args:
        object_name: Name of the object
        expiry_seconds: URL expiry time in seconds (default: 1 hour)
        
    Returns:
        str: Temporary URL for the object
    """
    swift = get_swift_client()
    container_name = get_container_name()
    
    try:
        # Get the temp URL key from Swift service
        temp_url_key = os.getenv("SWIFT_TEMP_URL_KEY")
        if not temp_url_key:
            # Try to get account metadata to find temp URL key
            headers = swift.head_account()
            temp_url_key = headers.get('x-account-meta-temp-url-key')
            if not temp_url_key:
                raise ValueError("Swift temp URL key not configured. Set SWIFT_TEMP_URL_KEY environment variable or configure account metadata.")
        
        # Generate temp URL
        from swiftclient.service import get_conn
        import time
        
        expires = int(time.time() + expiry_seconds)
        
        # Construct the temp URL
        storage_url = swift.get_auth()[0]
        path = f"/v1/{swift.get_auth()[1].split('/')[-1]}/{container_name}/{object_name}"
        
        import hmac
        import hashlib
        
        hmac_body = f"GET\n{expires}\n{path}"
        signature = hmac.new(
            temp_url_key.encode('utf-8'),
            hmac_body.encode('utf-8'),
            hashlib.sha1
        ).hexdigest()
        
        temp_url = f"{storage_url}{path}?temp_url_sig={signature}&temp_url_expires={expires}"
        
        return temp_url
        
    except Exception as e:
        logger.error(f"Failed to generate presigned URL for {object_name}: {e}")
        raise


def get_object_info(object_name):
    """Get metadata information about a Swift object
    
    Args:
        object_name: Name of the object
        
    Returns:
        dict: Object metadata
    """
    swift = get_swift_client()
    container_name = get_container_name()
    
    try:
        headers = swift.head_object(container_name, object_name)
        return headers
    except Exception as e:
        logger.error(f"Failed to get object info for {object_name}: {e}")
        raise