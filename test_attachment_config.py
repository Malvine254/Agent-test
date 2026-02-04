"""
Diagnostic script to test Teams bot attachment handling configuration
Run this to verify your bot is properly configured to handle file attachments
"""

import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

def check_environment():
    """Check if required environment variables are set"""
    print("=" * 60)
    print("ENVIRONMENT CONFIGURATION CHECK")
    print("=" * 60)
    
    required_vars = {
        "BOT_ID": "Bot Application ID (from Azure)",
        "SECRET_BOT_PASSWORD": "Bot Application Secret",
        "AZURE_OPENAI_API_KEY": "Azure OpenAI API Key",
        "AZURE_OPENAI_ENDPOINT": "Azure OpenAI Endpoint",
        "AZURE_OPENAI_MODEL_DEPLOYMENT_NAME": "Azure OpenAI Model Name"
    }
    
    all_good = True
    for var, description in required_vars.items():
        value = os.getenv(var)
        if value:
            masked_value = value[:8] + "..." if len(value) > 8 else "***"
            print(f"✓ {var}: {masked_value} ({description})")
        else:
            print(f"✗ {var}: NOT SET - {description}")
            all_good = False
    
    print()
    return all_good

def check_bot_token():
    """Test if we can obtain a bot access token"""
    print("=" * 60)
    print("BOT TOKEN ACQUISITION TEST")
    print("=" * 60)
    
    try:
        from config import Config
        from simple_file_handler import get_bot_access_token
        
        print(f"Bot ID: {Config.APP_ID[:8]}..." if Config.APP_ID else "Bot ID: NOT SET")
        print(f"Bot Password: {'***' if Config.APP_PASSWORD else 'NOT SET'}")
        print()
        
        print("Attempting to obtain bot access token...")
        token = get_bot_access_token()
        
        if token:
            print(f"✓ Successfully obtained token: {token[:20]}...")
            print(f"  Token length: {len(token)} characters")
            print(f"  Token type: Bearer")
            return True
        else:
            print("✗ Failed to obtain token")
            print()
            print("Possible issues:")
            print("  1. BOT_ID (APP_ID) not set correctly")
            print("  2. SECRET_BOT_PASSWORD not set correctly")
            print("  3. Bot not registered in Azure")
            print("  4. Network/firewall blocking Microsoft login endpoints")
            return False
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def check_manifest():
    """Check if manifest is properly configured"""
    print()
    print("=" * 60)
    print("MANIFEST CONFIGURATION CHECK")
    print("=" * 60)
    
    try:
        import json
        manifest_path = Path(__file__).parent / "appPackage" / "manifest.json"
        
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        
        bots = manifest.get("bots", [])
        if not bots:
            print("✗ No bots configured in manifest")
            return False
        
        bot = bots[0]
        supports_files = bot.get("supportsFiles", False)
        
        print(f"Bot ID placeholder: {bot.get('botId', 'NOT SET')}")
        print(f"Scopes: {', '.join(bot.get('scopes', []))}")
        print(f"Supports Files: {supports_files}")
        
        if supports_files:
            print()
            print("✓ Manifest is correctly configured for file attachments")
            return True
        else:
            print()
            print("✗ supportsFiles is NOT enabled in manifest")
            print("  Add '\"supportsFiles\": true' to the bot configuration")
            return False
            
    except Exception as e:
        print(f"✗ Error reading manifest: {e}")
        return False

def main():
    print()
    print("╔════════════════════════════════════════════════════════════╗")
    print("║  TEAMS BOT ATTACHMENT HANDLER - DIAGNOSTIC TOOL           ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print()
    
    # Load environment variables
    from dotenv import load_dotenv
    load_dotenv()
    env_path = Path(__file__).parent / "env" / ".env.local"
    if env_path.exists():
        load_dotenv(env_path, override=True)
    
    results = []
    
    # Run checks
    results.append(("Environment Variables", check_environment()))
    results.append(("Bot Token Acquisition", check_bot_token()))
    results.append(("Manifest Configuration", check_manifest()))
    
    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    for check_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {check_name}")
    
    print()
    
    if all(result[1] for result in results):
        print("╔════════════════════════════════════════════════════════════╗")
        print("║  ✓ ALL CHECKS PASSED                                      ║")
        print("║                                                            ║")
        print("║  Your bot is properly configured to handle attachments!   ║")
        print("╚════════════════════════════════════════════════════════════╝")
        print()
        print("Next steps:")
        print("  1. Deploy/run your bot")
        print("  2. Test uploading files in Teams (Desktop or Web)")
        print("  3. Wait 10-30 seconds after upload before sending message")
        print("  4. Check logs for: '✓ Successfully processed attachment'")
    else:
        print("╔════════════════════════════════════════════════════════════╗")
        print("║  ✗ SOME CHECKS FAILED                                     ║")
        print("║                                                            ║")
        print("║  Please fix the issues above before testing attachments  ║")
        print("╚════════════════════════════════════════════════════════════╝")
        print()
        print("Common fixes:")
        print("  • Set BOT_ID and SECRET_BOT_PASSWORD in .env file")
        print("  • Verify bot is registered in Azure Portal")
        print("  • Ensure 'supportsFiles: true' in manifest.json")
        print("  • Check that .env file is in project root")
        
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
