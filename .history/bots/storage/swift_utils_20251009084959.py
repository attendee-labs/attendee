"""
Swift object storage utilities using python-swiftclient (Working Implementation)
"""
import os
import logging
from swiftclient import client
from swiftclient.exceptions import ClientException

logger = logging.getLogger(__name__)


def get_swift_client():
    """Create and return a Swift client instance using the working authentication method"""
    auth_url = os.getenv("OS_AUTH_URL")
    
    # Check for application credentials first
    app_cred_id = os.getenv("OS_APPLICATION_CREDENTIAL_ID")
    app_cred_secret = os.getenv("OS_APPLICATION_CREDENTIAL_SECRET")
    
    # Check for username/password credentials
    username = os.getenv("OS_USERNAME")
    password = os.getenv("OS_PASSWORD")
    project_name = os.getenv("OS_PROJECT_NAME")
    
    if app_cred_id and app_cred_secret:
        # Use application credentials
        os_options = {
            "auth_type": "v3applicationcredential",
            "application_credential_id": app_cred_id,
            "application_credential_secret": app_cred_secret,
            "region_name": os.getenv("OS_REGION_NAME"),
            "interface": os.getenv("OS_INTERFACE", "public"),
        }
        
        # Add project ID if available (sometimes needed for application credentials)
        project_id = os.getenv("OS_PROJECT_ID")
        if project_id:
            os_options["project_id"] = project_id
            
        return client.Connection(
            authurl=auth_url,
            auth_version="3",
            os_options=os_options,
        )
    elif username and password and project_name:
        # Use username/password authentication
        return client.Connection(
            authurl=auth_url,
            auth_version="3",
            user=username,
            key=password,
            tenant_name=project_name,
            os_options={
                "region_name": os.getenv("OS_REGION_NAME"),
                "interface": os.getenv("OS_INTERFACE", "public"),
                "user_domain_name": "default",
                "project_domain_name": "default",
            },
        )
    else:
        raise ValueError("Swift credentials not configured. Please set either application credentials (OS_APPLICATION_CREDENTIAL_ID, OS_APPLICATION_CREDENTIAL_SECRET) or username/password (OS_USERNAME, OS_PASSWORD, OS_PROJECT_NAME)")


def get_container_name():
    """Get the Swift container name from environment variables"""
    return os.getenv("SWIFT_CONTAINER_MEETS", "transcript-meets")


def upload_file_to_swift(file_content_or_path, object_name):
    """Upload file to Swift storage

    Args:
        file_content_or_path: Either bytes content or path to file
        object_name: Name of the object in Swift storage
    
    Returns:
        str: The object name that was uploaded
    """
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        if isinstance(file_content_or_path, (bytes, str)):
            # If it's bytes or string content, upload directly
            if isinstance(file_content_or_path, str) and os.path.exists(file_content_or_path):
                # It's a file path
                with open(file_content_or_path, "rb") as file_obj:
                    swift_client.put_object(container_name, object_name, contents=file_obj)
            else:
                # It's content (bytes or string)
                swift_client.put_object(container_name, object_name, contents=file_content_or_path)
        else:
            # Assume it's a file path
            with open(file_content_or_path, "rb") as file_obj:
                swift_client.put_object(container_name, object_name, contents=file_obj)
        
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
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        swift_client.delete_object(container_name, object_name)
        logger.info(f"Successfully deleted {object_name} from Swift container {container_name}")
        return True
    except ClientException as e:
        if e.http_status == 404:
            logger.warning(f"Object {object_name} not found in Swift container {container_name}")
            return True
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
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        swift_client.head_object(container_name, object_name)
        return True
    except ClientException as e:
        if e.http_status == 404:
            return False
        else:
            logger.error(f"Error checking if object {object_name} exists: {e}")
            raise


def list_objects_in_container(prefix=""):
    """List objects in the Swift container
    
    Args:
        prefix: Optional prefix to filter objects
        
    Returns:
        List of object names
    """
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        headers, objects = swift_client.get_container(container_name, prefix=prefix)
        return [obj["name"] for obj in objects]
    except ClientException:
        logger.error(f"Failed to list objects in Swift container {container_name}")
        return []


def generate_presigned_url(object_name, expiry_seconds=3600):
    """Generate a URL for accessing a Swift object
    
    Args:
        object_name: Name of the object
        expiry_seconds: URL expiry time in seconds (not used in this implementation)
        
    Returns:
        str: URL for the object
    """
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        auth_response = swift_client.get_auth()
        storage_url = auth_response[0]
        object_url = f"{storage_url}/{container_name}/{object_name}"
        return object_url
        
    except Exception as e:
        logger.error(f"Failed to generate URL for {object_name}: {e}")
        raise


def download_file_from_swift(object_name):
    """Download file content from Swift storage
    
    Args:
        object_name: Name of the object to download
        
    Returns:
        bytes: File content
    """
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        headers, content = swift_client.get_object(container_name, object_name)
        return content
    except ClientException as e:
        if e.http_status == 404:
            raise FileNotFoundError(f"Object {object_name} not found in container {container_name}")
        raise


def get_object_info(object_name):
    """Get metadata information about a Swift object
    
    Args:
        object_name: Name of the object
        
    Returns:
        dict: Object metadata
    """
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        headers = swift_client.head_object(container_name, object_name)
        return headers
    except Exception as e:
        logger.error(f"Failed to get object info for {object_name}: {e}")
        raise