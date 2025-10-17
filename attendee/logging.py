from datetime import datetime, timezone

from pythonjsonlogger import jsonlogger


class ISOJsonFormatter(jsonlogger.JsonFormatter):
    """
    JSON formatter that adds ISO 8601 timestamp
    """

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)

        # Add ISO timestamp from the record's created time
        # record.created is a Unix timestamp (float)
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        log_record["timestamp"] = dt.isoformat()
