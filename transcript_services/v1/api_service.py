import os

import requests


def start_transcription(transcript_uuid):
    """
    Send a request to start transcribing an MP3 file from AWS S3.

    Args:
        transcript_uuid (str): The UUID of the transcript

    Returns:
        requests.Response: The response from the API
    """
    # API credentials
    api_key = os.getenv("TRANSCRIPT_API_KEY")

    if not api_key:
        raise ValueError("API key is not set in environment variables.")

    # API endpoint
    url = os.getenv("TRANSCRIPT_API_URL") + "/v1/record/done"

    if not url:
        raise ValueError("API URL is not set in environment variables.")

    # Request headers
    headers = {
        "x-api-key": api_key,
    }

    # Request parameters
    params = {"transcript_id": transcript_uuid}

    # Send POST request
    response = requests.post(url, headers=headers, params=params)

    # Check if request was successful
    if response.status_code == 200:
        print(f"Successfully started transcription for UUID: {transcript_uuid}")
    else:
        print(f"Error starting transcription: {response.status_code}")
        print(f"Response: {response.text}")

    return response


def could_not_record(transcript_id):
    # API credentials
    api_key = os.getenv("TRANSCRIPT_API_KEY")

    if not api_key:
        raise ValueError("API key is not set in environment variables.")

    # API endpoint
    url = os.getenv("TRANSCRIPT_API_URL") + "/v1/record/failed"

    if not url:
        raise ValueError("API URL is not set in environment variables.")

    # Request headers
    headers = {
        "x-api-key": api_key,
    }

    # Request parameters
    params = {"transcript_id": transcript_id}

    # Send POST request
    response = requests.post(url, headers=headers, params=params)

    # Check if request was successful
    if response.status_code == 200:
        print(f"Successfully informed of recording failure for UUID: {transcript_id}")
    else:
        print(f"Error informing of recording failure: {response.status_code}")
        print(f"Response: {response.text}")

    return response
