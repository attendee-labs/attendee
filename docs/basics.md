# Basics of Bots

## What is a bot?
In the Attendee platform, a bot is an automated participant that can join virtual meetings across Zoom, Google Meet, and Microsoft Teams to perform various tasks such as recording and transcription.

## Bot Capabilities

1. Recording: Bots can record audio and video from meetings
2. Transcription: Bots can transcribe meeting conversations in real-time
3. Speech: Bots can speak arbitrary audio into the meeting
4. Avatars: Bots can display arbitrary images from their virtual webcam
5. Chat: Bots can record and send chat messages

## Bot States
Bots go through these lifecycle states:

1. Ready: Initial state when bot is created
2. Joining: Bot is attempting to join meeting
3. Joined - Not Recording: Bot has joined but isn't recording
4. Joined - Recording: Bot has joined and is recording
5. Leaving: Bot is leaving the meeting
6. Post Processing: Bot is processing recordings
7. Fatal Error: Bot encountered an unrecoverable error
8. Waiting Room: Bot is in meeting's waiting room
9. Ended: Bot has completed all tasks and recordings and transcripts are available for download
10. Data Deleted: Bot data has been permanently deleted (recordings, transcripts, participants)

## Transcription Features

1. Realtime transcription
2. Multiple language support
3. Automatic language detection
4. Speaker identification / Diarization
5. Precise timestamps for each utterance
6. Ability to transcribe using third party providers or from platform closed captions

## Configuration Options
Bots can be configured with:

Transcription Settings
   - Language selection
   - Automatic language detection
   - Provider-specific options

Recording Settings
   - Recording type (Audio and Video / Audio Only)
   - Recording view (Speaker View / Gallery View)

Automatic leave settings
   - How long should the bot wait to be let into the meeting before giving up?
   - How long it should be silent before the bot leaves?
   - How long should the bot be the only one in the meeting before it leaves?
   - How long the meeting can last before the bot leaves?

Webhooks
   - Bot state changes
   - Transcript updates

## Platform Support
Currently supported platforms:
1. Zoom
2. Google Meet
3. Microsoft Teams

## Data Deletion
You can permanently delete all data associated with a bot, including recordings, transcripts, and participant information. 

To delete bot data, use the `POST /api/v1/bots/{bot_id}/delete_data` endpoint. This action:
- Is irreversible and cannot be undone
- Only works for bots in the `ended` or `fatal_error` states
- Preserves bot metadata for audit purposes
- Moves the bot to the `data_deleted` state

Note: Metadata fields (like bot ID, meeting URL, creation time) are retained for audit purposes even after data deletion.
