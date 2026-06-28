import logging

from .middleware import get_current_request_id


class RequestContextFormatter(logging.Formatter):
    default_extra = {
        "request_id": "",
        "event": "",
        "provider": "",
        "status": "",
        "duration_ms": "",
        "source_platform": "",
        "error_type": "",
    }

    def format(self, record):
        for key, value in self.default_extra.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        if not record.request_id:
            record.request_id = get_current_request_id()
        if not record.event:
            record.event = record.getMessage()
        return super().format(record)
