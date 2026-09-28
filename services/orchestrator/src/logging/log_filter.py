import logging
import re

# Broadened to catch 'apiKey', 'api_key', 'tavily_api_key', etc.
JSON_KEY_PATTERN = re.compile(r'("(?:[a-zA-Z0-9_]*api_?[kK]ey)"\s*:\s*)"([^"]+)"')
# Broadened to catch standard OpenAI (sk-...), Anthropic (sk-ant-...), etc.
SK_PATTERN = re.compile(r'(sk-[a-zA-Z0-9\-]{20,})')

def scrub_secrets_from_string(text: str) -> str:
    """Standalone string scrubber for use across the application (e.g., sockets)."""
    if not isinstance(text, str):
        return text
    scrubbed = JSON_KEY_PATTERN.sub(r'\1"********"', text)
    scrubbed = SK_PATTERN.sub('sk-********', scrubbed)
    return scrubbed

class SecretRedactingFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = scrub_secrets_from_string(record.msg)
        return True