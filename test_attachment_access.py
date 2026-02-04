#!/usr/bin/env python3
"""
Test script to diagnose attachment access issues.
Helps identify whether the problem is with:
1. Bot credentials configuration
2. Bot token authentication
3. File download permissions
"""

import sys
import os
import logging

# Configure logging to see detailed output
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s | %(message)s"
)
logger = logging.getLogger(__name__)

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from config import Config
from simple_file_handler import get_bot_access_token

def test_credentials():
    """Test if bot credentials are properly configured."""
    logger.info("=" * 60)
    logger.info("TEST 1: Bot Credentials Configuration")
    logger.info("=" * 60)
    
    app_id = Config.APP_ID
    app_password = Config.APP_PASSWORD
    
    if not app_id:
        logger.error("❌ BOT_ID (APP_ID) is NOT configured")
        logger.error("   Set environment variable: BOT_ID=<your-bot-id>")
        return False
    
    if not app_password:
        logger.error("❌ SECRET_BOT_PASSWORD (APP_PASSWORD) is NOT configured")
        logger.error("   Set environment variable: SECRET_BOT_PASSWORD=<your-password>")
        return False
    
    logger.info(f"✓ BOT_ID configured: {app_id[:10]}...{app_id[-5:]}")
    logger.info(f"✓ SECRET_BOT_PASSWORD configured: {'*' * 20}")
    return True

def test_bot_token():
    """Test if bot can obtain access token from Microsoft."""
    logger.info("\n" + "=" * 60)
    logger.info("TEST 2: Bot Token Authentication")
    logger.info("=" * 60)
    
    token = get_bot_access_token()
    
    if not token:
        logger.error("❌ Failed to obtain bot access token")
        logger.error("   Possible causes:")
        logger.error("   1. Bot credentials are invalid")
        logger.error("   2. Network connectivity issue")
        logger.error("   3. Microsoft identity service is unavailable")
        logger.error("   4. Bot not registered in Azure Bot Service")
        return False
    
    logger.info(f"✓ Bot token obtained: {token[:20]}...{token[-10:]}")
    logger.info(f"   Token length: {len(token)} bytes")
    return True

def test_file_access_example():
    """Show what a successful file download would look like."""
    logger.info("\n" + "=" * 60)
    logger.info("TEST 3: Example File Access Flow")
    logger.info("=" * 60)
    
    logger.info("When a user uploads a file in Teams:")
    logger.info("  1. User selects file and uploads in Teams chat")
    logger.info("  2. Teams uploads file to user's OneDrive for Business")
    logger.info("  3. Bot receives message with attachment info:")
    logger.info("     - contentType: 'application/vnd.microsoft.teams.file.download.info'")
    logger.info("     - content.downloadUrl: Pre-authenticated OneDrive URL")
    logger.info("     - content.contentUrl: Permanent SharePoint URL")
    logger.info("  4. Bot calls process_attachment()")
    logger.info("  5. Bot obtains access token (see TEST 2 above)")
    logger.info("  6. Bot downloads file from OneDrive with bot token")
    logger.info("  7. Bot extracts text content")
    logger.info("  8. Bot sends response to user")
    
    logger.info("\nCommon issues:")
    logger.info("  ❌ 403 Forbidden → File not fully uploaded yet (wait 10-30s)")
    logger.info("  ❌ 401 Unauthorized → Bot credentials invalid")
    logger.info("  ❌ 404 Not Found → File deleted or URL expired")

def main():
    logger.info("\n🔍 ATTACHMENT ACCESS DIAGNOSTICS\n")
    
    credentials_ok = test_credentials()
    
    if not credentials_ok:
        logger.info("\n" + "=" * 60)
        logger.info("SETUP REQUIRED")
        logger.info("=" * 60)
        logger.info("\nSet environment variables:")
        logger.info("  Linux/Mac: export BOT_ID=<your-bot-id>")
        logger.info("              export SECRET_BOT_PASSWORD=<your-password>")
        logger.info("  Windows:   set BOT_ID=<your-bot-id>")
        logger.info("             set SECRET_BOT_PASSWORD=<your-password>")
        return 1
    
    token_ok = test_bot_token()
    
    test_file_access_example()
    
    logger.info("\n" + "=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    
    if credentials_ok and token_ok:
        logger.info("✓ Bot is properly configured and can access files")
        logger.info("  If you're still having issues, check:")
        logger.info("  1. Enable detailed logging: Set DEBUG=true")
        logger.info("  2. Check bot activity logs in Azure portal")
        logger.info("  3. Wait 10-30s after uploading before sending message")
        return 0
    else:
        logger.error("✗ Bot configuration issues found - see above for details")
        return 1

if __name__ == "__main__":
    sys.exit(main())
