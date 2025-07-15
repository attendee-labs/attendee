import os
import tempfile
from urllib.parse import urljoin

from django.core.files.storage import Storage
from django.core.files.base import ContentFile
from django.utils.deconstruct import deconstructible
from swiftclient import client
from swiftclient.exceptions import ClientException


@deconstructible
class InfomaniakSwiftStorage(Storage):
    
    def __init__(self):
        self.auth_url = os.getenv('OS_AUTH_URL')
        self.app_cred_id = os.getenv('OS_APPLICATION_CREDENTIAL_ID')
        self.app_cred_secret = os.getenv('OS_APPLICATION_CREDENTIAL_SECRET')
        self.region = os.getenv('OS_REGION_NAME')
        # Use SWIFT_CONTAINER_AUDIO as the default container for Django file storage
        self.container_name = os.getenv('SWIFT_CONTAINER_AUDIO', 'transcript-audio')
        self._connection = None
    
    @property
    def connection(self):
        if self._connection is None:
            self._connection = client.Connection(
                authurl=self.auth_url,
                auth_version='3',
                os_options={
                    'auth_type': 'v3applicationcredential',
                    'application_credential_id': self.app_cred_id,
                    'application_credential_secret': self.app_cred_secret,
                    'region_name': self.region,
                    'interface': 'public',
                }
            )
        return self._connection
    
    def _open(self, name, mode='rb'):
        try:
            headers, content = self.connection.get_object(self.container_name, name)
            return ContentFile(content)
        except ClientException as e:
            if e.http_status == 404:
                raise FileNotFoundError(f"File not found: {name}")
            raise
    
    def _save(self, name, content):
        content.seek(0)
        self.connection.put_object(
            self.container_name,
            name,
            contents=content,
            content_type=getattr(content, 'content_type', 'application/octet-stream')
        )
        return name
    
    def delete(self, name):
        try:
            self.connection.delete_object(self.container_name, name)
        except ClientException as e:
            if e.http_status == 404:
                pass
            else:
                raise
    
    def exists(self, name):
        try:
            self.connection.get_object(self.container_name, name, resp_chunk_size=1)
            return True
        except ClientException as e:
            if e.http_status == 404:
                return False
            raise
    
    def listdir(self, path):
        try:
            headers, objects = self.connection.get_container(self.container_name, prefix=path)
            files = []
            dirs = set()
            
            for obj in objects:
                name = obj['name']
                if name.startswith(path):
                    relative_name = name[len(path):].lstrip('/')
                    if '/' in relative_name:
                        dir_name = relative_name.split('/')[0]
                        dirs.add(dir_name)
                    else:
                        files.append(relative_name)
            
            return list(dirs), files
        except ClientException:
            return [], []
    
    def size(self, name):
        try:
            headers = self.connection.head_object(self.container_name, name)
            return int(headers.get('content-length', 0))
        except ClientException as e:
            if e.http_status == 404:
                raise FileNotFoundError(f"File not found: {name}")
            raise
    
    def url(self, name):
        # Return a Swift URL format since we don't use S3-compatible endpoint
        return f"swift://{self.container_name}/{name}"
    
    def get_accessed_time(self, name):
        raise NotImplementedError("Swift storage doesn't support access time")
    
    def get_created_time(self, name):
        raise NotImplementedError("Swift storage doesn't support creation time")
    
    def get_modified_time(self, name):
        try:
            headers = self.connection.head_object(self.container_name, name)
            return None
        except ClientException as e:
            if e.http_status == 404:
                raise FileNotFoundError(f"File not found: {name}")
            raise
