import requests
import json

def main():
    status_url = "https://aios-trading-engine-production.up.railway.app/api/status"
    try:
        resp = requests.get(status_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Print only top-level fields (omitting trades list details for brevity)
        summary = {k: v for k, v in data.items() if k != "trades"}
        print(json.dumps(summary, indent=2))
        print(f"Total trades listed: {len(data.get('trades', []))}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
