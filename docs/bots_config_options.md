## Bots Configuration Options
Bots can be configured with:

1. Transcription Settings
   - Language selection
   - Automatic language detection
   - Deepgram-specific options

2. Recording Settings
   - Recording type (Audio and Video / Audio Only)
   - Recording view (Speaker View / Gallery View)

3. RTMP Streaming Settings
   - Destination URL (must start with rtmp:// or rtmps://)
   - Stream key

4. Debug Recording Settings
   - **Purpose**: Debug recordings capture the bot's activity while attempting to join or interact with a meeting. This can be useful for troubleshooting issues such as failed meeting joins or unexpected behavior.
   - **Configuration**:
     - Debug recordings can be enabled based on the meeting type or specific debug settings in the bot's configuration.
     - To explicitly enable debug recordings, add the following to the request's `settings` under the `debug_settings` key:
       ```json
       {
           "debug_settings": {
               "create_debug_recording": true
           }
       }
       ```
 - **Code Reference**:
     - The logic for determining whether a debug recording should be created is implemented in the `create_debug_recording` method of the `Bot` model in `models.py`.
