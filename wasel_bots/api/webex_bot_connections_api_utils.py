import logging

import requests

logger = logging.getLogger(__name__)


def _validate_webex_service_app_credentials(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """
    Validate Webex OAuth credentials by attempting to refresh the access token.
    
    Args:
        client_id: Webex Integration Client ID
        client_secret: Webex Integration Client Secret
        refresh_token: Webex OAuth Refresh Token
        
    Returns:
        dict: Validation result with success status, message, access_token, and expires_at
        
    Raises:
        Exception: If credentials are invalid
    """
    logger.info("Validating Webex OAuth credentials")
    logger.debug(f"Client ID length: {len(client_id)}, Refresh token length: {len(refresh_token)}")
    
    try:
        # Try to get an access token using refresh token grant
        logger.debug("Sending request to Webex token endpoint")
        resp = requests.post(
            "https://webexapis.com/v1/access_token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10
        )
        
        logger.debug(f"Webex API response status: {resp.status_code}")
        
        resp.raise_for_status()
        
        token_data = resp.json()
        logger.info("Successfully received access token from Webex")
        
        # Calculate expiration timestamp
        from django.utils import timezone
        expires_in = token_data.get("expires_in", 3600)
        expires_at = timezone.now().timestamp() + expires_in
        
        # If we got a token, credentials are valid
        return {
            "success": True,
            "message": "Credentials validated successfully",
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token", refresh_token),  # Some flows return new refresh token
            "expires_at": expires_at
        }
        
    except requests.exceptions.HTTPError as e:
        error_detail = ""
        try:
            error_detail = e.response.json()
            logger.error(f"Error validating Webex credentials: {e}, Response: {error_detail}")
        except:
            logger.error(f"Error validating Webex credentials: {e}, Response text: {e.response.text}")
        
        if e.response.status_code == 401:
            raise Exception("Invalid Client ID, Client Secret, or Refresh Token. Please check your credentials.")
        elif e.response.status_code == 400:
            error_msg = error_detail.get('message', '') if error_detail else ''
            if 'invalid_grant' in str(error_detail).lower() or 'invalid_grant' in e.response.text.lower():
                raise Exception("Invalid or expired Refresh Token. Please generate a new refresh token from your Webex Integration.")
            elif error_msg:
                raise Exception(f"Invalid credentials: {error_msg}")
            else:
                raise Exception("Invalid credentials format. Please verify your Client ID, Client Secret, and Refresh Token.")
        else:
            raise Exception(f"Error validating credentials (HTTP {e.response.status_code}): {e}")
    except requests.exceptions.Timeout:
        logger.error("Timeout while validating Webex credentials")
        raise Exception("Connection timeout. Please try again.")
    except requests.exceptions.ConnectionError:
        logger.error("Connection error while validating Webex credentials")
        raise Exception("Unable to connect to Webex API. Please check your network connection.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error while validating Webex credentials: {e}")
        raise Exception(f"Network error: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error validating Webex credentials: {e}")
        raise Exception(f"Error validating credentials: {str(e)}")
