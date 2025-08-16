from bots.serializers import CalendarSerializer, ChatMessageSerializer, ParticipantEventSerializer


def chat_message_webhook_payload(chat_message):
    return ChatMessageSerializer(chat_message).data


def utterance_webhook_payload(utterance):
    return {
        "speaker_name": utterance.participant.full_name,
        "speaker_uuid": utterance.participant.uuid,
        "speaker_user_uuid": utterance.participant.user_uuid,
        "timestamp_ms": utterance.timestamp_ms,
        "duration_ms": utterance.duration_ms,
        "transcription": {"transcript": utterance.transcription.get("transcript")} if utterance.transcription else None,
    }


def participant_event_webhook_payload(participant_event):
    return ParticipantEventSerializer(participant_event).data


def calendar_webhook_payload(calendar):
    serialized_calendar = CalendarSerializer(calendar).data
    return {
        "state": serialized_calendar["state"],
        "connection_failure_data": serialized_calendar["connection_failure_data"],
        "last_successful_sync_at": serialized_calendar["last_successful_sync_at"],
        "last_attempted_sync_at": serialized_calendar["last_attempted_sync_at"],
    }
