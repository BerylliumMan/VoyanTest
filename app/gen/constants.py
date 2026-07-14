"""Shared constants for the AI generation module."""

# Upload limits
ALLOWED_EXTENSIONS = {".docx", ".md", ".png", ".jpg", ".jpeg", ".pdf"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB per file
MAX_FILES = 10
MAX_TOTAL_SIZE = 50 * 1024 * 1024  # 50MB total

# Analysis limits
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries (exponential backoff)
