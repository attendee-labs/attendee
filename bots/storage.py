from django.core.files.storage import Storage, storages
from django.utils.deconstruct import deconstructible


@deconstructible
class StorageAlias(Storage):
    def __init__(self, alias: str):
        self.alias = alias

    @property
    def _wrapped(self):
        return storages[self.alias]

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    # Methods defined in base Storage class need explicit delegation
    # since __getattr__ is only called when the attribute isn't found in the MRO
    def delete(self, name):
        return self._wrapped.delete(name)
