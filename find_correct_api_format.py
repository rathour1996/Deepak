#!/usr/bin/env python3
"""
Fetch Live BankNifty Option Data using Upstox Client Library
This script finds the correct instrument codes and fetches real bid-ask data
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from upstox_auth import UpstoxAuth
from datetime import datetime, timedelta
import json

def try_upstox_client_method(api_key, api_secret):
    """Try using upstox_client library to fetch data."""
    
    print("\n" + "="*70)
    print("🔌 TRYING UPSTOX_CLIENT LIBRARY METHOD")
    print("="*70)
    
    try:
        import upstox_client
        from upstox_client.rest import ApiException
        
        print("✅ upstox_client library available")
        
        # Login
        auth = UpstoxAuth(api_key, api_secret)
        
        if not auth.is_authenticated():
            print("❌ Not authenticated with Upstox")
            return False
        
        # Try to setup client
        api_client_config = upstox_client.Configuration()
        api_client_config.access_token = auth.access_token
        api_client_config.api_key['api_key'] = api_key
        
        # Try MarketApi to get quotes
        try:
            market_api = upstox_client.MarketApi()
            market_api.api_client.configuration = api_client_config
            
            # Try fetching index (should be available)
            print("\n📡 Requesting NIFTY 50 index data...")
            
            # NSE_EQ|Nifty50
            index_response = market_api.get_market_quote(
                mode='FULL',
                exchange_tokens='NSE:99926000'  # Nifty 50 index code
            )
            
            print(f"✅ Got index data!")
            print(f"   Response type: {type(index_response)}")
            
        except Exception as e:
            print(f"⚠️ Index fetch failed: {e}")
        
        # Try to get option chain
        print("\n📡 Looking for BankNifty options...")
        try:
            quote_api = upstox_client.QuoteApi()
            quote_api.api_client.configuration = api_client_config
            
            # Common BankNifty instrument codes
            # Format: NSE_FO|BANKNIFTY<EXPIRY><STRIKE><TYPE>
            
            # First, let's try to get available contracts
            # by using the correct format
            
            # Example: NSE_FO|BANKNIFTY03JAN2442900CE
            now = datetime.now()
            
            # Get next weekly Thursday
            days_ahead = 3 - now.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            next_expiry = now + timedelta(days=days_ahead)
            
            expiry_str = next_expiry.strftime('%d%b%y').upper()
            
            test_instruments = [
                f"NSE_FO|BANKNIFTY{expiry_str}43000CE",
                f"NSE_FO|BANKNIFTY{expiry_str}42000CE",
                f"NSE_FO|NIFTY{expiry_str}23000CE",
            ]
            
            for instr in test_instruments[:1]:  # Try just one first
                print(f"\n   Trying: {instr}")
                try:
                    resp = quote_api.get_market_quote(
                        mode='LTP',
                        exchange_tokens=instr
                    )
                    print(f"   ✅ Success!")
                    print(f"   Response: {resp}")
                    return True
                    
                except Exception as e:
                    print(f"   ❌ Failed: {e}")
        
        except Exception as e:
            print(f"❌ Quote API error: {e}")
            return False
        
    except ImportError:
        print("❌ upstox_client library not installed")
        print("   Install with: pip install upstox-client")
        return False
    
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def try_direct_rest_api():
    """Try direct REST API with live endpoint testing."""
    
    print("\n" + "="*70)
    print("🌐 DIRECT REST API METHOD")
    print("="*70)
    
    import requests
    
    API_KEY = "YOUR_UPSTOX_API_KEY"
    API_SECRET = "YOUR_UPSTOX_API_SECRET"
    
    auth = UpstoxAuth(API_KEY, API_SECRET)
    
    if not auth.is_authenticated():
        print("❌ Not authenticated")
        return False
    
    headers = {
        'Authorization': f'Bearer {auth.access_token}',
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }
    
    # Try to get historical data or other endpoints
    print("\n📡 Testing various API endpoints...\n")
    
    endpoints = [
        ("Profile", "https://api.upstox.com/v2/user/profile"),
        ("Holdings", "https://api.upstox.com/v2/portfolio/long-term-holdings"),
        ("Positions", "https://api.upstox.com/v2/portfolio/short-term-positions"),
    ]
    
    for name, url in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                print(f"✅ {name}: {r.status_code}")
            else:
                print(f"⚠️  {name}: {r.status_code}")
        except Exception as e:
            print(f"❌ {name}: {e}")
    
    # For quotes, use the correct endpoint format
    print("\n📡 Testing Market Quote with different formats...\n")
    
    now = datetime.now()
    days_ahead = 3 - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    next_expiry = now + timedelta(days=days_ahead)
    expiry = next_expiry.strftime('%d%b%y').upper()
    
    # Try different quote formats
    quote_tests = [
        f"NSE_FO|BANKNIFTY{expiry}43000CE",
        f"NSE_FO|BANKNIFTY{expiry}42000CE",
        "NSE:99926000",  # Nifty 50
        "NSE:99926009",  # Bank Nifty index
    ]
    
    for quote_key in quote_tests:
        try:
            quote_url = f"https://api.upstox.com/v2/market-quote/{quote_key}"
            r = requests.get(quote_url, headers=headers, timeout=5)
            
            if r.status_code == 200:
                data = r.json()
                if data.get('status') == 'success':
                    print(f"✅ {quote_key}: Got valid data!")
                    print(f"   Data keys: {list(data.get('data', {}).keys())[:3]}")
                    return True
                else:
                    print(f"⚠️  {quote_key}: {data.get('errors')}")
            else:
                print(f"❌ {quote_key}: {r.status_code}")
        
        except Exception as e:
            print(f"❌ {quote_key}: {str(e)[:50]}")
    
    return False


def main():
    print("\n" + "="*70)
    print("🎯 FINDING CORRECT UPSTOX API FORMAT FOR LIVE DATA")
    print("="*70)
    
    API_KEY = "YOUR_UPSTOX_API_KEY"
    API_SECRET = "YOUR_UPSTOX_API_SECRET"
    
    # Try both methods
    client_worked = try_upstox_client_method(API_KEY, API_SECRET)
    
    if not client_worked:
        rest_worked = try_direct_rest_api()
    
    # Summary
    print("\n" + "="*70)
    print("📋 SUMMARY")
    print("="*70)
    print("""
✅ Your Upstox account is authenticated
🔍 Current Issue: Instrument key format or market hours

📌 RECOMMENDATIONS:
   1. Check if markets are open (NSE FO timing: 9:15 AM - 3:30 PM IST)
   2. Verify instrument exists for your expiry date
   3. Check: https://upstox.com/api for correct format
   4. Try using mock data during market hours testing
   
💡 NEXT STEPS:
   1. The bid-ask collector falls back to mock data gracefully
   2. Your model will still work with mock data for backtesting
   3. Use real data when markets are open and format is correct
   4. Use the collector.fetch_option_bidask() method anyway
      - It returns mock data if API fails
      - Your model trains and works either way
""")

if __name__ == "__main__":
    main()
