from cryptography.fernet import Fernet
from django.core.management.utils import get_random_secret_key


def generate_encryption_key():
    return Fernet.generate_key().decode("utf-8")


def generate_django_secret_key():
    return get_random_secret_key()


def main():
    credentials_key = generate_encryption_key()
    django_key = generate_django_secret_key()

    print(f"CREDENTIALS_ENCRYPTION_KEY={credentials_key}")
    print(f"DJANGO_SECRET_KEY={django_key}")
    print("STORAGE_PROTOCOL=minio")
    print("MINIO_ENDPOINT_URL=http://minio:9000")
    print("MINIO_PUBLIC_ENDPOINT_URL=http://localhost:9000")
    print("MINIO_ACCESS_KEY=minioadmin")
    print("MINIO_SECRET_KEY=minioadmin123")
    print("MINIO_RECORDING_STORAGE_BUCKET_NAME=attendee-recordings")


if __name__ == "__main__":
    main()
