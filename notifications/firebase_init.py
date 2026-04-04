"""
Firebase Admin SDK initialization for notifications.

Priority:
1. Credentials DB row (CredentialTypes.FIREBASE_FCM, project=WASEL_PROJECT_ID)
2. FIREBASE_CREDENTIALS_JSON env var (inline JSON)
3. FIREBASE_SERVICE_ACCOUNT_PATH env var (file path)
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_firebase_app = None


def _load_from_db() -> dict | None:
    try:
        project_object_id = os.getenv("WASEL_PROJECT_ID")
        if not project_object_id:
            return None

        from bots.models import Credentials, Project

        project = Project.objects.filter(object_id=project_object_id).first()
        if not project:
            logger.warning("Firebase init: project '%s' not found", project_object_id)
            return None

        row = Credentials.objects.filter(
            project=project,
            credential_type=Credentials.CredentialTypes.FIREBASE_FCM,
        ).first()
        if not row:
            return None

        payload = row.get_credentials() or {}
        service_account = payload.get("service_account_json")
        if not service_account:
            logger.warning("Firebase init: FIREBASE_FCM credential exists but missing service_account_json")
            return None

        logger.info("Firebase init: loaded service account from DB")
        return service_account
    except Exception:
        logger.exception("Firebase init: failed reading credentials from DB")
        return None


def get_firebase_app():
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    try:
        import firebase_admin
        from firebase_admin import credentials
        from django.conf import settings

        if firebase_admin._apps:
            _firebase_app = firebase_admin.get_app()
            return _firebase_app

        cert = None

        db_sa = _load_from_db()
        if db_sa:
            cert = credentials.Certificate(db_sa)
        elif getattr(settings, "FIREBASE_CREDENTIALS_JSON", "") or os.getenv("FIREBASE_CREDENTIALS_JSON", ""):
            raw_json = getattr(settings, "FIREBASE_CREDENTIALS_JSON", "") or os.getenv("FIREBASE_CREDENTIALS_JSON", "")
            cert = credentials.Certificate(json.loads(raw_json))
            logger.info("Firebase init: loaded from FIREBASE_CREDENTIALS_JSON")
        elif getattr(settings, "FIREBASE_SERVICE_ACCOUNT_PATH", "") or os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", ""):
            path = getattr(settings, "FIREBASE_SERVICE_ACCOUNT_PATH", "") or os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "")
            cert = credentials.Certificate(path)
            logger.info("Firebase init: loaded from FIREBASE_SERVICE_ACCOUNT_PATH")
        else:
            logger.warning("Firebase init: no credentials configured")
            return None

        _firebase_app = firebase_admin.initialize_app(cert)
        logger.info("Firebase init: initialized")
        return _firebase_app
    except Exception:
        logger.exception("Firebase init: initialization failed")
        return None


def reset_firebase_app() -> None:
    global _firebase_app
    try:
        import firebase_admin

        if _firebase_app is not None:
            firebase_admin.delete_app(_firebase_app)
    except Exception:
        pass
    _firebase_app = None
