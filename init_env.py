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
    print("# Sentry Configuration")
    print("DISABLE_SENTRY=true")
    print("# OpenStack Configuration for Infomaniak Swift Storage")
    print("OS_AUTH_TYPE=v3applicationcredential")
    print("OS_AUTH_URL=https://api.pub1.infomaniak.cloud/identity")
    print("OS_IDENTITY_API_VERSION=3")
    print("OS_REGION_NAME=dc4-a")
    print("OS_INTERFACE=public")
    print("OS_APPLICATION_CREDENTIAL_ID=")
    print("OS_APPLICATION_CREDENTIAL_SECRET=")
    print("OS_PROJECT_ID=2f82cc139c3a45908223757dc424e4ce")
    print("OS_PROJECT_NAME=PCP-S6XJACW")
    print("SWIFT_CONTAINER_MEETS=transcript-meets")
    print("TRANSCRIPT_API_URL=http://host.docker.internal:8000/api")
    print("TRANSCRIPT_API_KEY=")


if __name__ == "__main__":
    main()
