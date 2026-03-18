#!/usr/bin/env python3
"""
Upstox API Diagnostic Tool
Checks authentication and API connectivity
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from upstox_auth import UpstoxAuth
import json

def main():
    print("\n" + "="*70)
    print("🔍 UPSTOX API DIAGNOSTIC")
    print("="*70)

    API_KEY = "YOUR_UPSTOX_API_KEY"
    API_SECRET = "YOUR_UPSTOX_API_SECRET"
    REDIRECT_URI = "http://localhost:8501"

    # Create auth
    print("\n1️⃣ Checking authentication...")
    auth = UpstoxAuth(API_KEY, API_SECRET, REDIRECT_URI)

    if not auth.is_authenticated():
        print("❌ Not authenticated")
        print(f"\nPlease authorize first:")
        print(f"   {auth.get_login_url()}\n")
        return False

    print("✅ Authenticated")
    print(f"   Access Token: {auth.access_token[:32]}...")
    print(f"   Token Type: {type(auth.access_token)}")
    print(f"   Token Length: {len(auth.access_token)}")

    # Try simple API call
    print("\n2️⃣ Testing API endpoint...")

    try:
        # Try fetching a simple instrument
        import requests

        headers = {
            'Authorization': f'Bearer {auth.access_token}',
            'Accept': 'application/json',
            'User-Agent': 'BankNifty-Trading-Bot'
        }

        # Test endpoint: Get profile (should work)
        test_url = "https://api.upstox.com/v2/user/profile"
        print(f"   Testing: {test_url}")

        response = requests.get(test_url, headers=headers, timeout=10)
        print(f"   Status Code: {response.status_code}")

        if response.status_code == 200:
            print("✅ Profile API working")
            data = response.json()
            if data.get('status') == 'success':
                user = data.get('data', {})
                print(f"   User: {user.get('preference', {}).get('language', 'N/A')}")
        else:
            print(f"⚠️ API returned: {response.status_code}")
            print(f"   Response: {response.text[:200]}")

    except Exception as e:
        print(f"❌ API test failed: {e}")

    # Try market quote
    print("\n3️⃣ Testing Market Quote API...")

    try:
        # For testing, try with a standard index option
        # Format: NSE_FO|<SYMBOL><EXPIRY><STRIKE><TYPE>
        # But we need correct expiry format

        test_instruments = [
            "NSE_FO|BANKNIFTY05Mar2643000CE",  # Try format 1
            "NSE_FO|BANKNIFTY05MAR2643000CE",  # Try format 2 (caps)
            "NSE_FO|BANKNIFTY05MAR26CE43000",  # Try format 3
        ]

        for instr_key in test_instruments:
            print(f"\n   Trying: {instr_key}")

            quote_url = f"https://api.upstox.com/v2/market-quote/{instr_key}"
            response = requests.get(quote_url, headers=headers, timeout=10)

            print(f"   Status: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    print(f"   ✅ Got quote data!")
                    print(f"      Response keys: {list(data.get('data', {}).keys())}")
                    break
                else:
                    print(f"   ❌ Error: {data.get('errors')}")
            elif response.status_code == 400:
                print(f"   ❌ Bad Request - wrong format")
            elif response.status_code == 401:
                print(f"   ❌ Unauthorized - token issue")
                print(f"   Attempting to refresh token...")
                if auth.refresh_access_token():
                    headers['Authorization'] = f'Bearer {auth.access_token}'
                    print(f"   ✅ Token refreshed")

    except Exception as e:
        print(f"❌ Market quote test failed: {e}")

    # Summary
    print("\n" + "="*70)
    print("📋 DIAGNOSTIC SUMMARY")
    print("="*70)
    print("""
✅ Authentication: Working
🔍 Next Steps:
   1. Verify instrument key  format with Upstox docs
   2. Check API rate limits (might need to wait)
   3. Ensure token permissions allow market data
   4. Try using upstox_client library instead

📚 Useful Resources:
   - Upstox API Docs: https://upstox.com/api
   - Instrument Format: NSE_FO|<SYMBOL><DDMMMYY><STRIKE><TYPE>
   - Example: NSE_FO|BANKNIFTY04MAR2643000CE
""")

if __name__ == "__main__":
    main()
