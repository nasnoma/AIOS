import time
import requests
from loguru import logger

def get_with_retry(url: str, params: dict = None, timeout: int = 10, max_retries: int = 5, backoff_factor: float = 2.0) -> requests.Response:
    """Make a GET request with automatic retry on 429 rate limits (Too Many Requests)."""
    delay = 2.0
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 429:
                logger.warning(f"Rate limit (429) hit on {url}. Waiting {delay}s before retry (attempt {attempt + 1}/{max_retries})...")
                time.sleep(delay)
                delay *= backoff_factor
                continue
            return resp
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                raise e
            logger.warning(f"Request error: {e}. Retrying in {delay}s...")
            time.sleep(delay)
            delay *= backoff_factor
    
    # Final try to succeed or raise the error
    return requests.get(url, params=params, timeout=timeout)
