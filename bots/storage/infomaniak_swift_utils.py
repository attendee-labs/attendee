import os
from swiftclient import client
from swiftclient.exceptions import ClientException


def get_swift_client():
    return client.Connection(
        authurl=os.getenv('OS_AUTH_URL'),
        auth_version='3',
        os_options={
            'auth_type': 'v3applicationcredential',
            'application_credential_id': os.getenv('OS_APPLICATION_CREDENTIAL_ID'),
            'application_credential_secret': os.getenv('OS_APPLICATION_CREDENTIAL_SECRET'),
            'region_name': os.getenv('OS_REGION_NAME'),
            'interface': 'public',
        }
    )


def get_container_name():
    # Use SWIFT_CONTAINER_AUDIO as the default container for audio files
    return os.getenv('SWIFT_CONTAINER_AUDIO', 'transcript-audio')


def upload_file_to_swift(file_content_or_path, object_name):
    """Upload file to Swift storage
    
    Args:
        file_content_or_path: Either bytes content or path to file
        object_name: Name of the object in Swift storage
    """
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    if isinstance(file_content_or_path, (bytes, str)):
        # If it's bytes or string content, upload directly
        if isinstance(file_content_or_path, str) and os.path.exists(file_content_or_path):
            # It's a file path
            with open(file_content_or_path, 'rb') as file_obj:
                swift_client.put_object(
                    container_name,
                    object_name,
                    contents=file_obj
                )
        else:
            # It's content (bytes or string)
            swift_client.put_object(
                container_name,
                object_name,
                contents=file_content_or_path
            )
    else:
        # Assume it's a file path
        with open(file_content_or_path, 'rb') as file_obj:
            swift_client.put_object(
                container_name,
                object_name,
                contents=file_obj
            )
    
    return object_name


def delete_file_from_swift(object_name):
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        swift_client.delete_object(container_name, object_name)
        return True
    except ClientException as e:
        if e.http_status == 404:
            return True
        raise


def generate_presigned_url(object_name, expiry_seconds=3600):
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    auth_response = swift_client.get_auth()
    storage_url = auth_response[0]
    
    object_url = f"{storage_url}/{container_name}/{object_name}"
    
    return object_url


def list_objects_in_container(prefix=""):
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        headers, objects = swift_client.get_container(container_name, prefix=prefix)
        return [obj['name'] for obj in objects]
    except ClientException:
        return []


def object_exists(object_name):
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        swift_client.head_object(container_name, object_name)
        return True
    except ClientException as e:
        if e.http_status == 404:
            return False
        raise


def download_file_from_swift(object_name):
    """Download file content from Swift storage"""
    swift_client = get_swift_client()
    container_name = get_container_name()
    
    try:
        headers, content = swift_client.get_object(container_name, object_name)
        return content
    except ClientException as e:
        if e.http_status == 404:
            raise FileNotFoundError(f"Object {object_name} not found in container {container_name}")
        raise
