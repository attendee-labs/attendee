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
    print("AWS_RECORDING_STORAGE_BUCKET_NAME=attendee-recordings")
    print("AWS_ACCESS_KEY_ID=minioadmin")
    print("AWS_SECRET_ACCESS_KEY=minioadmin")
    print("AWS_DEFAULT_REGION=us-east-1")
    print("AWS_ENDPOINT_URL=http://minio:9000")


if __name__ == "__main__":
    main()
