from urllib.parse import urlparse

from ..common import XhsParseError, host_matches, is_blocked_hostname


class BasePlatformAdapter:
    platform_id = ""
    display_name = ""
    page_host_suffixes = ()
    media_host_suffixes = ()
    headers = {}

    def supports_url(self, url):
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if is_blocked_hostname(parsed.hostname):
            return False
        return host_matches(parsed.hostname, self.page_host_suffixes)

    def parse_url(self, url):
        raise NotImplementedError

    def parse_html(self, html, source_url=None):
        raise NotImplementedError

    def require_supported_url(self, url):
        if not self.supports_url(url):
            raise XhsParseError(f"请输入正确的{self.display_name or self.platform_id}链接")
