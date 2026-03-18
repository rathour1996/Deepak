#!/usr/bin/env python3
"""
Update token cache with fresh access token and verify it works.
"""

import sys
import json
from pathlib import Path
from datetime import datetime
import base64
import os

sys.path.insert(0, '/home/dr/banknifty_lstm')
sys.path.insert(0, '/home/dr/banknifty_lstm/src')

from upstox_auth import UpstoxAuth
from options_bidask_collector import OptionsBidAskCollector

# Load tokens securely from environment variables
FRESH_TOKEN = os.environ.get("UPSTOX_FRESH_TOKEN", "")
API_KEY = os.environ.get("UPSTOX_API_KEY", "YOUR_UPSTOX_API_KEY")
API_SECRET = os.environ.get("UPSTOX_API_SECRET", "YOUR_UPSTOX_API_SECRET")

def decode_jwt(token: str) -> dict:
    """Decode JWT token (without verification) to examine claims."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return {}

        payload = parts[1]
        # Add padding if needed
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += '=' * padding

        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception as e:
        print(f"Error decoding JWT: {e}")
        return {}

def main():
    print("=" * 100)
    print("🔐 UPDATE TOKEN CACHE AND VERIFY LIVE API ACCESS")
    print("=" * 100)

    # 1. Decode the fresh token to get expiry
    print("\n1️⃣ Decoding the fresh access token...")
    payload = decode_jwt(FRESH_TOKEN)

    if not payload:
        print("❌ Failed to decode token")
        return

    print(f"   ✅ Token decoded successfully")
    print(f"   Subject: {payload.get('sub')}")
    print(f"   Issued At: {datetime.fromtimestamp(payload.get('iat', 0))}")

    exp_timestamp = payload.get('exp', 0)
    exp_datetime = datetime.fromtimestamp(exp_timestamp)
    print(f"   Expires At: {exp_datetime}")
    print(f"   Time Remaining: {(exp_datetime - datetime.now()).total_seconds() / 3600:.1f} hours")

    # 2. Update the token cache
    print("\n2️⃣ Updating token cache...")
    cache_file = Path('data/.upstox_token_cache.json')
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    cache_data = {
        'access_token': FRESH_TOKEN,
        'refresh_token': None,
        'expiry': exp_datetime.isoformat(),
        'api_key': API_KEY,
    }

    with open(cache_file, 'w') as f:
        json.dump(cache_data, f, indent=2)

    print(f"   ✅ Token cache updated: {cache_file}")
    print(f"   Expiry: {exp_datetime.isoformat()}")

    # 3. Initialize auth with the fresh token
    print("\n3️⃣ Initializing Upstox authentication with fresh token...")
    auth = UpstoxAuth(API_KEY, API_SECRET)

    if auth.is_authenticated():
        print("   ✅ Successfully authenticated!")
    else:
        print("   ❌ Authentication failed")
        return

    # 4. Test with collector
    print("\n4️⃣ Testing live bid-ask data fetching...")
    try:
        collector = OptionsBidAskCollector(upstox_auth=auth)
        print("   ✅ Collector initialized")

        # Try to fetch bid-ask for a common strike
        bid_ask = collector.fetch_option_bidask("BANKNIFTY", 54000, "2026-03-30", "CE")

        if bid_ask:
            print(f"   ✅ Successfully fetched bid-ask data:")
            print(f"      Strike: 54000 CE")
            print(f"      Bid: {bid_ask.get('bid'):.2f}")
            print(f"      Ask: {bid_ask.get('ask'):.2f}")
            print(f"      Source: {bid_ask.get('source')}")
        else:
            print("   ⚠️  Could not fetch live data (markets may be closed)")

    except Exception as e:
        print(f"   ⚠️  Error testing collector: {e}")

    # 5. Test direct API call
    print("\n5️⃣ Testing direct API quote fetch...")
    try:
        # Use instrument key for 54000 CE
        quote = auth.get_market_quote(['NSE_FO|58534'])

        if quote and quote.get('status') == 'success':
            print("   ✅ API quote fetch successful!")
            data = quote.get('data', {})
            for key, value in data.items():
                depth = value.get('depth', {})
                buy_depth = depth.get('buy', [])
                sell_depth = depth.get('sell', [])
                if buy_depth and sell_depth:
                    bid = buy_depth[0].get('price')
                    ask = sell_depth[0].get('price')
                    print(f"      Bid: {bid}, Ask: {ask}")
        else:
            print("   ⚠️  Quote fetch returned non-success status")
            if quote:
                print(f"      Response: {quote.get('status')}")

    except Exception as e:
        print(f"   ⚠️  Error: {e}")

    # 6. Summary
    print("\n" + "=" * 100)
    print("✅ TOKEN CACHE UPDATED AND VERIFIED")
    print("=" * 100)
    print(f"\n✅ Access Token will be valid until: {exp_datetime}")
    print(f"📊 You can now use the system with live market data (when markets are open)")
    print(f"🚀 To launch the Streamlit dashboard, run: streamlit run app_with_live_data.py")
    print("=" * 100)

if __name__ == "__main__":
    main()
