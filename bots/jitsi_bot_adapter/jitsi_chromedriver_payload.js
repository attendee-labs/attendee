// Jitsi platform payload — phase 1 stub.
//
// Phase 3 implements the full contract here, mirroring the Google Meet payload:
// - class WebSocketClient + window.ws (sendJson, enableMediaSending, disableMediaSending)
// - window.styleManager with getMeetingAudioStream() for the media recorder
// - window.botOutputManager = new BotOutputManager({...}) (class comes from the shared payload)
// - window.sendChatMessage(text) via APP.conference._room.sendTextMessage
// - APP.conference event bridging: USER_JOINED/USER_LEFT/DISPLAY_NAME_CHANGED -> UsersUpdate,
//   DOMINANT_SPEAKER_CHANGED -> ParticipantSpeechStartStopEvent, MESSAGE_RECEIVED -> ChatMessage,
//   conference end/kick -> MeetingStatusChange, mixed audio via AudioContext -> AudioFormatUpdate + binary frames.
//
// All python-side calls into these globals use optional chaining, so this stub is safe to load.
