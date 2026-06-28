"""
Security utilities for webhook validation and authentication
"""
import hmac
import hashlib
from typing import Optional
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import settings


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    """Verify API key for protected endpoints"""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )
    
    # In production, validate against database or secure storage
    # For now, we'll use a simple check
    if api_key != settings.SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )
    
    return api_key


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify webhook signature from TradingView or other sources
    
    Args:
        payload: Raw request body
        signature: Signature from header
        secret: Shared secret key
    
    Returns:
        True if signature is valid
    """
    expected_signature = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(signature, expected_signature)


def validate_tradingview_webhook(
    payload: bytes,
    signature: Optional[str] = None
) -> bool:
    """
    Validate TradingView webhook request
    
    Args:
        payload: Raw webhook payload
        signature: Optional signature header
    
    Returns:
        True if valid
    
    Raises:
        HTTPException if validation fails
    """
    if not signature:
        # If no signature provided, check if we require it
        if settings.ENVIRONMENT == "production":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Webhook signature required in production",
            )
        return True
    
    if not verify_webhook_signature(
        payload,
        signature,
        settings.TRADINGVIEW_WEBHOOK_SECRET
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook signature",
        )
    
    return True
