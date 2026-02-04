"""
Network Connectivity Diagnostic Tool
Tests connectivity to required Microsoft services for Teams Bot
"""

import urllib.request
import ssl
import socket
import sys
from datetime import datetime

def test_connection(url, service_name, timeout=10):
    """Test connection to a URL and return status"""
    print(f"\n{'='*60}")
    print(f"Testing: {service_name}")
    print(f"URL: {url}")
    print(f"{'='*60}")
    
    try:
        # Create request with headers
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
        # Try connection
        start_time = datetime.now()
        with urllib.request.urlopen(req, timeout=timeout) as response:
            duration = (datetime.now() - start_time).total_seconds()
            
            print(f"✓ SUCCESS")
            print(f"  Status: {response.status}")
            print(f"  Response Time: {duration:.2f}s")
            
            if duration > 3:
                print(f"  ⚠️ WARNING: Slow response ({duration:.2f}s)")
            
            return True
            
    except urllib.error.HTTPError as e:
        print(f"✗ HTTP ERROR")
        print(f"  Status Code: {e.code}")
        print(f"  Reason: {e.reason}")
        return False
        
    except urllib.error.URLError as e:
        print(f"✗ CONNECTION ERROR")
        if hasattr(e, 'reason'):
            if isinstance(e.reason, socket.timeout):
                print(f"  Reason: Connection timeout after {timeout}s")
            elif isinstance(e.reason, ConnectionRefusedError):
                print(f"  Reason: Connection refused")
            elif isinstance(e.reason, OSError) and "WinError 10054" in str(e.reason):
                print(f"  Reason: Connection forcibly closed by remote host")
                print(f"  This often indicates:")
                print(f"    - Firewall blocking the connection")
                print(f"    - VPN interference")
                print(f"    - Rate limiting by the server")
            else:
                print(f"  Reason: {e.reason}")
        else:
            print(f"  Error: {e}")
        return False
        
    except socket.timeout:
        print(f"✗ TIMEOUT")
        print(f"  Connection timed out after {timeout}s")
        return False
        
    except Exception as e:
        print(f"✗ UNEXPECTED ERROR")
        print(f"  Type: {type(e).__name__}")
        print(f"  Message: {e}")
        return False


def main():
    print("\n" + "="*60)
    print("MICROSOFT TEAMS BOT - NETWORK CONNECTIVITY CHECK")
    print("="*60)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Services to test
    tests = [
        ("https://login.microsoftonline.com", "Microsoft Authentication (JWKS)"),
        ("https://api.botframework.com", "Bot Framework API"),
        ("https://graph.microsoft.com", "Microsoft Graph API"),
        ("https://smba.trafficmanager.net", "Bot Service Endpoint"),
    ]
    
    results = []
    for url, name in tests:
        success = test_connection(url, name)
        results.append((name, success))
    
    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    
    passed = sum(1 for _, success in results if success)
    total = len(results)
    
    for name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"{status:8} - {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed < total:
        print("\n⚠️ ISSUES DETECTED")
        print("\nCommon solutions:")
        print("  1. Check your internet connection")
        print("  2. Disable VPN and try again")
        print("  3. Check Windows Firewall settings")
        print("  4. Check corporate proxy/firewall")
        print("  5. Try running as Administrator")
        print("  6. Restart your network adapter")
        
        print("\nIf issues persist:")
        print("  - Contact your IT department")
        print("  - Check Microsoft 365 service status:")
        print("    https://status.office365.com/")
        
        sys.exit(1)
    else:
        print("\n✓ All connectivity tests passed!")
        print("Your network connection to Microsoft services is working properly.")
        sys.exit(0)


if __name__ == "__main__":
    main()
