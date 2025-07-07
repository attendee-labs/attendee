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
    print("INFOMANIAK_ENDPOINT_URL=https://s3.pub1.infomaniak.cloud")
    print("INFOMANIAK_REGION=sbg")
    print("INFOMANIAK_ACCESS_KEY_ID=")
    print("INFOMANIAK_SECRET_ACCESS_KEY=")
    print("INFOMANIAK_RECORDING_BUCKET_NAME=")


if __name__ == "__main__":
    main()
