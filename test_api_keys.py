import requests
from config import OPENEXCHANGERATES_API_KEY, CURRENCYLAYER_API_KEY

# Test Open Exchange Rates
print("Testing Open Exchange Rates API...")
url_openex = f"https://openexchangerates.org/api/latest.json?app_id={OPENEXCHANGERATES_API_KEY}&base=USD&symbols=EUR"
response_openex = requests.get(url_openex)
print(response_openex.json())

# Test Currency Layer
print("\nTesting Currency Layer API...")
url_currencylayer = f"http://apilayer.net/api/live?access_key={CURRENCYLAYER_API_KEY}&currencies=EUR&source=USD"
response_currencylayer = requests.get(url_currencylayer)
print(response_currencylayer.json())

# Test ExchangeRate-API (free tier, no key required)
print("\nTesting ExchangeRate-API...")
url_exchangerate = "https://open.er-api.com/v6/latest/USD"
response_exchangerate = requests.get(url_exchangerate)
print(response_exchangerate.json()) 