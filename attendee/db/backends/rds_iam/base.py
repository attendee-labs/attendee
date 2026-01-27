import os
import boto3
from django.db.backends.postgresql.base import DatabaseWrapper as PostgresDatabaseWrapper


class DatabaseWrapper(PostgresDatabaseWrapper):
    """
    Django DB backend that uses RDS IAM auth tokens as the password.
    Credentials come from the pod's IAM role (IRSA), so no static secrets needed.
    """

    def get_new_connection(self, conn_params):
        host = conn_params.get("host")
        port = int(conn_params.get("port") or 5432)
        user = conn_params.get("user")

        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        )
        if not region:
            raise RuntimeError("AWS_REGION or AWS_DEFAULT_REGION must be set for RDS IAM auth")

        # Generate an auth token (valid 15 min) to use as the password. :contentReference[oaicite:12]{index=12}
        rds = boto3.client("rds", region_name=region)
        token = rds.generate_db_auth_token(
            DBHostname=host,
            Port=port,
            DBUsername=user,
        )

        conn_params["password"] = token

        # Enforce TLS
        conn_params.setdefault("sslmode", "verify-full")
        conn_params.setdefault("sslrootcert", "/etc/ssl/certs/rds-global-bundle.pem")

        return super().get_new_connection(conn_params)
