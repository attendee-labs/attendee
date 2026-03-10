import base64
import json
import re
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

import tldextract

from .models import (
    MeetingTypes,
)

HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def contains_multiple_urls(url: str):
    if not url:
        return False

    found_urls = []
    # Iterate over every suffix of the url
    for i in range(len(url)):
        suffix = url[i:]
        # Check if the suffix is a valid url via the regexp
        if HTTP_URL_RE.match(suffix):
            found_urls.append(suffix)
            continue
        # Check the unquoted suffix
        if HTTP_URL_RE.match(unquote(suffix)):
            found_urls.append(unquote(suffix))
            continue
        # Check the double unquoted suffix
        if HTTP_URL_RE.match(unquote(unquote(suffix))):
            found_urls.append(unquote(unquote(suffix)))
            continue
        # Check the base64 decoded suffix
        try:
            if HTTP_URL_RE.match(base64.b64decode(suffix).decode("utf-8")):
                found_urls.append(base64.b64decode(suffix).decode("utf-8"))
                continue
        except Exception:
            # Skip if not valid base64 or can't be decoded as UTF-8
            pass

    return len(found_urls) > 1


def root_domain_from_url(url):
    if not url:
        return None
    return tldextract.extract(url).registered_domain


def domain_and_subdomain_from_url(url):
    if not url:
        return None
    extract_from_url = tldextract.extract(url)
    return extract_from_url.subdomain + "." + extract_from_url.registered_domain


def meeting_type_from_url(url):
    meeting_type, normalized_url = normalize_meeting_url(url)
    return meeting_type


def normalize_teams_url(conversation_id, message_id, tenant_id, organizer_id):
    return f'https://teams.microsoft.com/l/meetup-join/{conversation_id}/{message_id}?context={{"Tid":"{tenant_id}","Oid":"{organizer_id}"}}'


def normalize_meeting_url(url):
    if not url:
        return None, None

    url = url.strip().rstrip(">")

    for _ in range(3):
        meeting_type, normalized_url = normalize_meeting_url_raw(url)
        if meeting_type is not None and normalized_url is not None and not contains_multiple_urls(normalized_url):
            return meeting_type, normalized_url

        url = unquote(url)

    return None, None


def normalize_meeting_url_raw(url):
    # Returns (meeting_type, normalized_url)
    if not url:
        return None, None

    root_domain = root_domain_from_url(url)
    domain_and_subdomain = domain_and_subdomain_from_url(url)

    if root_domain == "zoom.us":
        # Parse the URL and keep only the 'pwd' query parameter
        parsed_url = urlparse(url)
        if not parsed_url.scheme:
            parsed_url = urlparse(f"https://{url}")
        query_params = parse_qs(parsed_url.query)

        # Sanitize the path - extract valid path up to first invalid character
        sanitized_path = parsed_url.path
        valid_path_match = re.match(r"^([a-zA-Z0-9/_-]*)", sanitized_path)
        if valid_path_match:
            sanitized_path = valid_path_match.group(1)

        # Ensure path starts with / and normalize multiple slashes
        if not sanitized_path.startswith("/"):
            sanitized_path = "/" + sanitized_path
        sanitized_path = re.sub(r"/+", "/", sanitized_path)
        # Keep only the 'pwd' parameter if it exists and sanitize it
        filtered_params = {}
        if "pwd" in query_params:
            # Zoom passwords follow pattern: alphanumeric characters, optionally followed by .digits
            pwd_value = query_params["pwd"][0]  # Get first value from list
            zoom_pwd_pattern = r"^([a-zA-Z0-9]+(?:\.\d+)?)"
            match = re.match(zoom_pwd_pattern, pwd_value)
            if match:
                # Extract only the valid password part, ignoring any trailing text
                sanitized_pwd = match.group(1)
                filtered_params["pwd"] = [sanitized_pwd]
            # If password doesn't match expected pattern, skip it for security
        if "tk" in query_params:
            # Registrant token for meetings or webinars that require registration
            tk_value = query_params["tk"][0]
            if tk_value and tk_value.strip():
                filtered_params["tk"] = [tk_value.strip()]

        # Reconstruct the URL with sanitized path and only the pwd/tk parameters
        new_query = "&".join([f"{key}={value[0]}" for key, value in filtered_params.items()])
        normalized_url = urlunparse(("https", parsed_url.netloc, sanitized_path, "", new_query, ""))

        # There must be an integer meeting ID in the path
        meeting_id_match = re.search(r"(\d+)", sanitized_path)
        if not meeting_id_match or not meeting_id_match.group(1):
            return None, None

        return MeetingTypes.ZOOM, normalized_url

    # Check if it's a Google Meet URL
    if domain_and_subdomain == "meet.google.com":
        # Use regex to extract the meeting code from Google Meet URL
        # Meeting code is the part after meet.google.com/
        google_meet_match = re.search(r"meet\.google\.com/([a-zA-Z0-9-]+)", url)
        if google_meet_match:
            meeting_code = google_meet_match.group(1)
            normalized_url = f"https://meet.google.com/{meeting_code}"
            return MeetingTypes.GOOGLE_MEET, normalized_url

    if domain_and_subdomain == "teams.microsoft.com" or domain_and_subdomain == "teams.live.com":
        # Teams URL format: https://teams.microsoft.com/l/meetup-join/<conversation_id>/<message_id>?context={"Tid":"<tenant_id>","Oid":"<organizer_id>"}
        # Robustly handles various Teams URL patterns that may appear before /l/meetup-join/ such as:
        # - https://teams.microsoft.com/v2/?meetingjoin=true#/l/meetup-join/...
        # - https://teams.microsoft.com/some/other/path#/l/meetup-join/...
        # - https://teams.microsoft.com/l/meetup-join/... (direct format)
        teams_match = re.search(r"teams\.(?:microsoft\.com|live\.com)(?:/[^/]*)*?/l/meetup-join/([^/]+)/([^?]+)\?context=.*?\"Tid\":\"([^\"]+)\".*?\"Oid\":\"([^\"]+)\"", url)

        if teams_match:
            conversation_id = teams_match.group(1)
            message_id = teams_match.group(2)
            tenant_id = teams_match.group(3)
            organizer_id = teams_match.group(4)

            # Construct normalized URL with extracted components
            return MeetingTypes.TEAMS, normalize_teams_url(conversation_id, message_id, tenant_id, organizer_id)

        # Handle Teams launcher URLs like:
        # https://teams.microsoft.com/dl/launcher/launcher.html?url=/_#/l/meetup-join/19:meeting_...@thread.v2/0?context={"Tid":"...","Oid":"..."}&...
        teams_launcher_match = re.search(r"teams\.microsoft\.com/dl/launcher/launcher\.html\?url=/_#/l/meetup-join/([^/]+)/([^?]+)\?context=.*?\"Tid\":\"([^\"]+)\".*?\"Oid\":\"([^\"]+)\"", url)

        if teams_launcher_match:
            conversation_id = teams_launcher_match.group(1)
            message_id = teams_launcher_match.group(2)
            tenant_id = teams_launcher_match.group(3)
            organizer_id = teams_launcher_match.group(4)

            # Construct normalized URL with extracted components
            return MeetingTypes.TEAMS, normalize_teams_url(conversation_id, message_id, tenant_id, organizer_id)

        # Handle Teams light meetings URLs with coordinates:
        # https://teams.microsoft.com/light-meetings/launch?agent=web&version=...&coords=<base64_encoded_json>&...
        teams_light_meetings_match = re.search(r"teams\.microsoft\.com/light-meetings/launch\?.*coords=([^&]+)", url)

        if teams_light_meetings_match:
            try:
                # Extract and decode the coords parameter
                coords_param = teams_light_meetings_match.group(1)
                # URL decode first if needed
                coords_param = unquote(coords_param)
                # Base64 decode
                decoded_coords = base64.b64decode(coords_param).decode("utf-8")
                # Parse JSON
                coords_data = json.loads(decoded_coords)

                # Extract required fields from the JSON
                conversation_id = coords_data.get("conversationId")
                tenant_id = coords_data.get("tenantId")
                organizer_id = coords_data.get("organizerId")
                message_id = coords_data.get("messageId", "0")  # Default to '0' if not present

                if conversation_id and tenant_id and organizer_id:
                    # Construct normalized URL with extracted components
                    return MeetingTypes.TEAMS, normalize_teams_url(conversation_id, message_id, tenant_id, organizer_id)

            except (ValueError, KeyError, json.JSONDecodeError):
                # If decoding or parsing fails, continue to next pattern
                pass

        # Handle Teams URLs with format: https://teams.<domain>.com/meet/<meeting_id>?p=<passcode>
        teams_live_meetings_match = re.search(r"teams\.([^.]+\.com)(?:/[^/]*)*?/meet/([^?]+)\?p=([^&\s]+)", url)

        if teams_live_meetings_match:
            domain = teams_live_meetings_match.group(1)  # e.g., "live.com" or "microsoft.com"
            meeting_id = teams_live_meetings_match.group(2)
            passcode = teams_live_meetings_match.group(3)

            if domain == "live.com" or domain == "microsoft.com":
                # Create canonical URL format - using the extracted components
                # We'll use a consistent format regardless of the original domain
                canonical_url = f"https://teams.{domain}/meet/{meeting_id}?p={passcode}"
                return MeetingTypes.TEAMS, canonical_url

        # Handle Teams launcher URLs with format:
        # https://teams.live.com/dl/launcher/launcher.html?url=/_#/meet/<meeting_id>?p=<passcode>&anon=true&type=meet&...
        # https://teams.microsoft.com/dl/launcher/launcher.html?url=/_#/meet/<meeting_id>?p=<passcode>&anon=true&type=meet&...
        teams_launcher_meetings_match = re.search(r"teams\.([^.]+\.com)/dl/launcher/launcher\.html\?url=/_#/meet/([^?]+)\?p=([^&\s]+)", url)

        if teams_launcher_meetings_match:
            domain = teams_launcher_meetings_match.group(1)  # e.g., "live.com" or "microsoft.com"
            meeting_id = teams_launcher_meetings_match.group(2)
            passcode = teams_launcher_meetings_match.group(3)

            if domain == "live.com" or domain == "microsoft.com":
                # Create canonical URL format using the extracted domain
                canonical_url = f"https://teams.{domain}/meet/{meeting_id}?p={passcode}"
                return MeetingTypes.TEAMS, canonical_url

    return None, None


# Returns (meeting_id, password, registrant_token) from a Zoom join URL.
def parse_zoom_join_url(join_url):
    # Parse the URL into components
    parsed = urlparse(join_url)

    # Extract meeting ID using regex to match only numeric characters
    meeting_id_match = re.search(r"(\d+)", parsed.path)
    meeting_id = meeting_id_match.group(1) if meeting_id_match else None

    # Extract password and registrant token from query parameters
    query_params = parse_qs(parsed.query)
    password = query_params.get("pwd", [None])[0]
    registrant_token = query_params.get("tk", [None])[0]

    return (meeting_id, password, registrant_token)
