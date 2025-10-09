"""
Django storage backend for OpenStack Swift
"""
import os
import logging
from datetime import datetime
from io import BytesIO
from urllib.parse import urljoin

from django.conf import settings
from django.core.files import File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

from .swift_utils import (
    get_swift_client,
    get_container_name,
    upload_file_to_swift,
    delete_file_from_swift,
    object_exists,
    generate_presigned_url,
    get_object_info,
    list_objects_in_container,
)

logger = logging.getLogger(__name__)


@deconstructible
class SwiftStorage(Storage):
    """
    Django Storage backend for OpenStack Swift object storage
    """
    
    def __init__(self, container_name=None, **kwargs):
        self.container_name = container_name or get_container_name()
        self.base_url = getattr(settings, 'SWIFT_BASE_URL', None)
        
    def _open(self, name, mode='rb'):
        """
        Retrieves the specified file from storage.
        """
        try:
            swift = get_swift_client()
            headers, content = swift.get_object(self.container_name, name)
            
            if isinstance(content, bytes):
                file_content = BytesIO(content)
            else:
                # If content is an iterator, read it all
                file_content = BytesIO(b''.join(content))
                
            file_content.name = name
            file_content.mode = mode
            return File(file_content)
            
        except Exception as e:
            logger.error(f"Failed to open file {name} from Swift: {e}")
            raise IOError(f"Failed to open file {name} from Swift: {e}")
    
    def _save(self, name, content):
        """
        Saves new content to the file specified by name.
        """
        try:
            # Read the content
            if hasattr(content, 'read'):
                file_content = content.read()
            else:
                file_content = content
                
            # Upload to Swift
            logger.debug(f"Saving file {name} to Swift...")
            upload_file_to_swift(file_content, name)
            
            return name
            
        except Exception as e:
            logger.error(f"Failed to save file {name} to Swift: {e}")
            raise IOError(f"Failed to save file {name} to Swift: {e}")
    
    def delete(self, name):
        """
        Deletes the specified file from the storage system.
        """
        try:
            delete_file_from_swift(name)
        except Exception as e:
            logger.error(f"Failed to delete file {name} from Swift: {e}")
            raise IOError(f"Failed to delete file {name} from Swift: {e}")
    
    def exists(self, name):
        """
        Returns True if a file referenced by the given name already exists in the
        storage system, or False if the name is available for a new file.
        """
        try:
            return object_exists(name)
        except Exception as e:
            logger.error(f"Failed to check if file {name} exists in Swift: {e}")
            return False
    
    def listdir(self, path):
        """
        Lists the contents of the specified path, returning a 2-tuple of lists;
        the first item being directories, the second item being files.
        """
        try:
            objects = list_objects_in_container(prefix=path)
            
            directories = set()
            files = []
            
            for obj in objects:
                name = obj['name']
                if path and not name.startswith(path):
                    continue
                    
                # Remove the prefix
                relative_name = name[len(path):] if path else name
                relative_name = relative_name.lstrip('/')
                
                if '/' in relative_name:
                    # This is in a subdirectory
                    dir_name = relative_name.split('/')[0]
                    directories.add(dir_name)
                else:
                    # This is a file in the current directory
                    files.append(relative_name)
            
            return list(directories), files
            
        except Exception as e:
            logger.error(f"Failed to list directory {path} in Swift: {e}")
            return [], []
    
    def size(self, name):
        """
        Returns the total size, in bytes, of the file specified by name.
        """
        try:
            headers = get_object_info(name)
            return int(headers.get('content-length', 0))
        except Exception as e:
            logger.error(f"Failed to get size of file {name} from Swift: {e}")
            raise IOError(f"Failed to get size of file {name} from Swift: {e}")
    
    def url(self, name):
        """
        Returns an absolute URL where the file's contents can be accessed
        directly by a Web browser.
        """
        try:
            # Generate a temporary URL with default expiry
            return generate_presigned_url(name, expiry_seconds=3600)
        except Exception as e:
            logger.error(f"Failed to generate URL for file {name}: {e}")
            if self.base_url:
                return urljoin(self.base_url, f"{self.container_name}/{name}")
            return None
    
    def get_accessed_time(self, name):
        """
        Returns the last accessed time (as datetime object) of the file
        specified by name.
        """
        # Swift doesn't track access time, return modified time instead
        return self.get_modified_time(name)
    
    def get_created_time(self, name):
        """
        Returns the creation time (as datetime object) of the file
        specified by name.
        """
        # Swift doesn't track creation time separately, return modified time
        return self.get_modified_time(name)
    
    def get_modified_time(self, name):
        """
        Returns the last modified time (as datetime object) of the file
        specified by name.
        """
        try:
            headers = get_object_info(name)
            last_modified = headers.get('last-modified')
            if last_modified:
                # Parse the HTTP date format
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(last_modified)
            return None
        except Exception as e:
            logger.error(f"Failed to get modified time for file {name}: {e}")
            return None
    
    def get_available_name(self, name, max_length=None):
        """
        Returns a filename that's free on the target storage system, and
        available for new content to be written to.
        """
        if max_length is None:
            max_length = getattr(settings, 'FILE_UPLOAD_MAX_MEMORY_SIZE', None)
            
        # If the filename already exists, modify it
        if self.exists(name):
            dir_name, file_name = os.path.split(name)
            file_root, file_ext = os.path.splitext(file_name)
            
            # Keep trying until we find an available name
            count = 1
            while True:
                new_name = f"{file_root}_{count}{file_ext}"
                if dir_name:
                    new_name = os.path.join(dir_name, new_name)
                    
                if max_length and len(new_name) > max_length:
                    # Truncate the root name to make room for the suffix
                    truncate_length = max_length - len(f"_{count}{file_ext}")
                    if dir_name:
                        truncate_length -= len(dir_name) + 1
                    file_root = file_root[:truncate_length]
                    new_name = f"{file_root}_{count}{file_ext}"
                    if dir_name:
                        new_name = os.path.join(dir_name, new_name)
                
                if not self.exists(new_name):
                    return new_name
                    
                count += 1
                
        return name