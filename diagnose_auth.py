#!/usr/bin/env python3
"""
Diagnose Upstox OAuth Authentication Issues
Tests each step of the auth flow to identify the problem
"""

import sys
sys.path.insert(0, '/home/dr/banknifty_lstm/src')

import base64
import requests
import json
from upstox_auth import UpstoxAuth

print("=" * 100)
print("🔍 UPSTOX OAUTH AUTHENTICATION TROUBLESHOOTING")
print("=" * 100)

# Your credentials
api_key = "YOUR_UPSTOX_API_KEY"
api_secret = "YOUR_UPSTOX_API_SECRET"
redirect_uri = "http://localhost:8501"

print("\n1️⃣ CREDENTIALS CHECK")
print("-" * 100)
print(f"API Key: {api_key[:20]}...")
print(f"API Secret: {api_secret[:5]}...")
print(f"Redirect URI: {redirect_uri}")

# Test Basic Auth encoding
auth_string = f"{api_key}:{api_secret}"
auth_bytes = auth_string.encode('utf-8')
auth_base64 = base64.b64encode(auth_bytes).decode('utf-8')
print(f"✅ Basic Auth encoded: {auth_base64[:30]}...")

print("\n2️⃣ CHECKING CACHED TOKEN")
print("-" * 100)

try:
    auth = UpstoxAuth(api_key, api_secret, redirect_uri)

    if auth.is_authenticated():
        print(f"✅ Cached token is VALID")
        print(f"   Token: {auth.access_token[:50]}...")
        print(f"   Expiry: {auth.token_expiry}")
        print(f"\n⚠️ You don't need to re-authenticate!")
        print(f"   Your cached token is still valid and should work.")
        print(f"   If you want to force a new auth, run:")
        print(f"   >> import os; os.remove('data/.upstox_token_cache.json')")
    else:
        print(f"❌ Cached token is INVALID or EXPIRED")
        print(f"   You'll need to re-authenticate.")
except Exception as e:
    print(f"❌ Error checking cached token: {e}")

print("\n3️⃣ OAUTH ENDPOINT CHECK")
print("-" * 100)

# Check if endpoints are reachable
endpoints = {
    "Authorization": "https://api.upstox.com/v2/login/authorization/dialog",
    "Token Exchange": "https://api.upstox.com/v2/login/authorization/token",
    "Profile": "https://api.upstox.com/v2/user/profile"
}

for name, url in endpoints.items():
    try:
        response = requests.head(url, timeout=5)
        print(f"✅ {name}: {response.status_code}")
    except Exception as e:
        print(f"❌ {name}: {str(e)[:50]}")

print("\n4️⃣ AUTHENTICATION FLOW")
print("-" * 100)
print("""
If you need to re-authenticate, follow these steps:

STEP 1: Get Authorization URL
   >>> from src.upstox_auth import UpstoxAuth
   >>> auth = UpstoxAuth("API_KEY", "API_SECRET")
   >>> url = auth.get_login_url()
   >>> print(url)

STEP 2: Visit URL in browser
   - Click the link printed above
   - Login with your Upstox account
   - You'll be redirected to a URL like:
     http://localhost:8501/?code=AUTH_CODE

STEP 3: Extract the AUTH_CODE from URL
   - Copy the 'code' parameter value

STEP 4: Exchange code for token
   >>> auth.generate_access_token("AUTH_CODE")
   - If successful: Token is cached and ready to use
   - If error: Check error message below

IMPORTANT NOTES:
   ⚠️ Auth codes expire in ~10 minutes
   ⚠️ Each auth code can only be used ONCE
   ⚠️ Redirect URI must match EXACTLY what's in Upstox dashboard
   ⚠️ If in doubt, delete cache and start fresh fresh
""")

print("\n5️⃣ COMMON SOLUTIONS")
print("-" * 100)
print("""
Problem: "400 Bad Request" on token exchange
Solutions:
   a) Auth code expired → Get a fresh code and try again within 10 minutes
   b) Redirect URI mismatch → Check that redirect_uri matches Upstox dashboard
   c) Already used → Can't reuse auth codes, get a fresh one
   d) Wrong credentials → Double-check API key and secret

Problem: "Cached token is INVALID"
Solutions:
   a) Token expired → Delete cache: rm data/.upstox_token_cache.json
   b) Re-authenticate with fresh code (see STEP 1-4 above)

Problem: "Not connected" in Streamlit
Solutions:
   a) Click "Connect Upstox Account" button in sidebar
   b) Follow auth flow step-by-step
   c) Check browser console for errors
   d) Try incognito/private mode if cookies issue

Problem: "Token keeps expiring"
Solutions:
   a) Use the auto-refresh feature (already built-in)
   b) Token refreshes automatically every 50 minutes
   c) If still failing, delete cache and re-auth
""")

print("\n6️⃣ CHECK IF SYSTEM IS WORKING")
print("-" * 100)

try:
    from upstox_auth import UpstoxAuth
    from options_bidask_collector import OptionsBidAskCollector

    auth = UpstoxAuth(api_key, api_secret, redirect_uri)

    if auth.is_authenticated():
        print("✅ Authentication: OK")

        # Test collector
        collector = OptionsBidAskCollector(upstox_auth=auth)
        print("✅ Collector: OK")

        # Test data fetch
        data = collector.fetch_option_bidask(
            symbol="BANKNIFTY",
            strike=54000,
            expiry="2026-03-30",
            option_type="CE"
        )

        if data:
            print("✅ Data Fetch: OK")
            print(f"   Bid: {data['bid']:.2f}")
            print(f"   Ask: {data['ask']:.2f}")
            print(f"   Source: {data.get('source', 'unknown')}")
            print("\n🎉 System is fully operational!")
        else:
            print("⚠️ Data Fetch: Failed (returned None)")
    else:
        print("❌ Authentication: NOT AUTHENTICATED")
        print("   → Follow steps 1-4 above to re-authenticate")

except Exception as e:
    print(f"❌ Error: {e}")
    print(f"   Check the error message above for details")

print("\n" + "=" * 100)
print("END OF DIAGNOSTICS")
print("=" * 100)
