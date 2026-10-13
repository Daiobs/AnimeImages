"""Private, bounded JSON observations. Not a public response contract."""

import datetime
import json
import math
import re

MAX_BYTES = 16 * 1024 * 1024
SECRET_FIELD = re.compile(r'cookie|authorization|password|passwd|secret|api.?key|credential|session|headers?|^proxy$|^phone$|^account$|^username$|^access_token$|^refresh_token$|^auth[_-]?token$', re.I)
SECRET_TEXT = re.compile(r'Bearer\s+\S+|(?:xkeysib|xsmtpsib)-\S+|-----BEGIN .*PRIVATE KEY-----', re.I)


class Capture:
    def __init__(self, limit=MAX_BYTES, secrets=()):
        self.limit = limit
        self.secrets = tuple(value for value in secrets if isinstance(value, str) and value)
        self.records = []
        self.bytes = 0
        self.nodes = 0
        self.redactions = 0
        self.truncated = False
        self.omissions = []
        self.attempts = 0

    def attempt(self):
        self.attempts += 1

    def plain(self, value, depth=0, seen=None):
        seen = set() if seen is None else seen
        self.nodes += 1
        if depth > 40 or self.nodes > 250000:
            self.truncated = True
            return {'_omitted': 'depth_or_node_limit'}
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float:
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            for secret in self.secrets:
                if secret in value:
                    value = value.replace(secret, '[REDACTED]')
                    self.redactions += 1
            if SECRET_TEXT.search(value):
                self.redactions += 1
                return '[REDACTED]'
            if len(value.encode()) > self.limit:
                self.truncated = True
                return {'_omitted': 'string_byte_limit', 'characters': len(value)}
            return value
        if isinstance(value, (datetime.date, datetime.datetime)):
            return value.isoformat()
        if id(value) in seen:
            return {'_omitted': 'cycle'}
        seen.add(id(value))
        try:
            if isinstance(value, dict):
                out = {}
                for key, item in value.items():
                    if not isinstance(key, (str, int)):
                        continue
                    key = str(key)
                    if SECRET_FIELD.search(key):
                        self.redactions += 1
                        out[key] = {'_redacted': type(item).__name__}
                    else:
                        out[key] = self.plain(item, depth + 1, seen)
                    if self.nodes > 250000:
                        break
                return out
            if isinstance(value, (list, tuple)):
                out = []
                for item in value:
                    out.append(self.plain(item, depth + 1, seen))
                    if self.nodes > 250000:
                        break
                return out
            # Read stored attributes only: never properties, model_dump or lazy iterators.
            try:
                attributes = object.__getattribute__(value, '__dict__')
            except (AttributeError, TypeError):
                attributes = None
            if isinstance(attributes, dict):
                return {'_class': type(value).__name__, 'fields': self.plain(
                    {k: v for k, v in attributes.items() if not k.startswith('_')}, depth + 1, seen)}
            self.omissions.append(type(value).__name__)
            return {'_not_exported_type': type(value).__name__}
        finally:
            seen.remove(id(value))

    def __call__(self, value):
        exported = self.plain(value)
        size = len(json.dumps(exported, ensure_ascii=True).encode())
        if self.bytes + size > self.limit - 32768:
            self.truncated = True
            self.omissions.append('record_byte_limit')
            return
        self.records.append(exported)
        self.bytes += size

    def summary(self):
        return {'records': self.records, 'retained_bytes': self.bytes,
                'truncated': self.truncated, 'redacted_fields': self.redactions,
                'not_exported_types': sorted(set(self.omissions)),
                'completeness': 'bounded_export' if self.truncated or self.omissions else 'returned_object_export'}
