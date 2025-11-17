"""
Python Matrix bridge backend for WhatsAppX.

This package exposes the FastAPI application via ``python_server.http_api:create_app``.
"""

from .http_api import create_app  # noqa: F401
