from typing import Optional

class MeetingServiceError(Exception):
    """Base exception for meeting service operations."""
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.message = message
        self.status_code = status_code

class MeetingConfigurationError(MeetingServiceError):
    """Raised when required configurations or credentials are missing."""
    def __init__(self, message: str = "Meeting BaaS API key is not configured"):
        super().__init__(message=message, status_code=500)

class MeetingProviderNetworkError(MeetingServiceError):
    """Raised when network connection or timeout occurs with Meeting BaaS."""
    def __init__(self, message: str):
        super().__init__(message=message, status_code=502)

class MeetingProviderResponseError(MeetingServiceError):
    """Raised when Meeting BaaS returns an error status code."""
    def __init__(self, status_code: int, detail: str):
        super().__init__(message=f"Meeting BaaS error ({status_code}): {detail}", status_code=status_code)

class BotNotFoundError(MeetingServiceError):
    """Raised when the specified bot ID is not found."""
    def __init__(self, bot_id: str):
        super().__init__(message=f"Bot with ID '{bot_id}' not found", status_code=404)
