import time
import requests
from loguru import logger
from trading_engine.config import settings
from trading_engine.storage import db

class BambooClient:
    def __init__(self):
        self.base_url = settings.bamboo_base_url.rstrip("/")
        self.client_token = None
        self.token_expiry = 0.0

    def _get_api_prefix(self, symbol: str = None, asset_class = None) -> str:
        from trading_engine.market_hours import classify_symbol, AssetClass
        ac = asset_class
        if not ac and symbol:
            ac = classify_symbol(symbol)
        if ac == AssetClass.BAMBOO_US_STOCK:
            return "us"
        return "ng"

    def _get_headers(self, include_auth=True) -> dict:
        headers = {
            "content-type": "application/json",
            "accept-language": "en",
        }
        if include_auth:
            token = self.get_client_token()
            headers.update({
                "x-client-token": token,
                "x-user-id": settings.bamboo_user_id,
                "x-subject-type": settings.bamboo_subject_type,
                "x-request-source": settings.bamboo_username,
            })
        return headers

    def get_client_token(self) -> str:
        now = time.time()
        # Refresh 5 minutes before expiry
        if not self.client_token or now >= self.token_expiry - 300:
            self._login()
        return self.client_token

    def _login(self):
        url = f"{self.base_url}/oauth/token"
        headers = {
            "content-type": "application/json",
            "accept-language": "en",
            "app-key": settings.bamboo_api_key
        }
        payload = {
            "username": settings.bamboo_username,
            "password": settings.bamboo_password
        }
        logger.info(f"Authenticating tenant with Bamboo API: {settings.bamboo_username}...")
        start_time = time.time()
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            duration_ms = (time.time() - start_time) * 1000.0
            
            try:
                db.log_api_call(
                    endpoint="/oauth/token",
                    method="POST",
                    params={"username": settings.bamboo_username},
                    status_code=resp.status_code,
                    response=resp.text[:500],
                    duration_ms=duration_ms
                )
            except Exception as e_db:
                logger.debug(f"Failed to log auth API call: {e_db}")
                
            resp.raise_for_status()
            data = resp.json()
            self.client_token = data["access_token"]
            expires_in = float(data.get("expires_in", 86400))
            self.token_expiry = time.time() + expires_in
            logger.success("Successfully authenticated with Bamboo API.")
        except Exception as e:
            logger.error(f"Failed to authenticate with Bamboo API: {e}")
            raise

    def get_portfolio_breakdown(self, asset_class=None) -> dict:
        prefix = self._get_api_prefix(asset_class=asset_class)
        url = f"{self.base_url}/api/lsx/{prefix}/portfolio/breakdown"
        headers = self._get_headers()
        start_time = time.time()
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            duration_ms = (time.time() - start_time) * 1000.0
            try:
                db.log_api_call(
                    endpoint=f"/api/lsx/{prefix}/portfolio/breakdown",
                    method="GET",
                    params={},
                    status_code=resp.status_code,
                    response=resp.text[:500],
                    duration_ms=duration_ms
                )
            except Exception as e_db:
                logger.debug(f"Failed to log portfolio API call: {e_db}")
                
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch Bamboo portfolio breakdown: {e}")
            raise

    def get_my_stocks(self, asset_class=None) -> dict:
        prefix = self._get_api_prefix(asset_class=asset_class)
        url = f"{self.base_url}/api/lsx/{prefix}/my_stocks"
        headers = self._get_headers()
        start_time = time.time()
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            duration_ms = (time.time() - start_time) * 1000.0
            try:
                db.log_api_call(
                    endpoint=f"/api/lsx/{prefix}/my_stocks",
                    method="GET",
                    params={},
                    status_code=resp.status_code,
                    response=resp.text[:500],
                    duration_ms=duration_ms
                )
            except Exception as e_db:
                logger.debug(f"Failed to log my_stocks API call: {e_db}")
                
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch Bamboo active holdings: {e}")
            raise

    def get_stock(self, symbol: str) -> dict:
        # Strip internal suffix if present
        clean_symbol = symbol.split("/")[0].split(":")[0].upper()
        prefix = self._get_api_prefix(symbol=symbol)
        url = f"{self.base_url}/api/lsx/{prefix}/stocks/{clean_symbol}"
        headers = self._get_headers()
        start_time = time.time()
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            duration_ms = (time.time() - start_time) * 1000.0
            try:
                db.log_api_call(
                    endpoint=f"/api/lsx/{prefix}/stocks/{clean_symbol}",
                    method="GET",
                    params={},
                    status_code=resp.status_code,
                    response=resp.text[:500],
                    duration_ms=duration_ms
                )
            except Exception as e_db:
                logger.debug(f"Failed to log get_stock API call: {e_db}")
                
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch details for Bamboo stock {clean_symbol}: {e}")
            raise

    def calculate_order(self, symbol: str, side: str, quantity: float, price: float) -> dict:
        clean_symbol = symbol.split("/")[0].split(":")[0].upper()
        prefix = self._get_api_prefix(symbol=symbol)
        url = f"{self.base_url}/api/lsx/{prefix}/order/calculate"
        currency = "USD" if prefix == "us" else "NGN"
        headers = self._get_headers()
        headers["currency"] = currency
        payload = {
            "type": "MARKET",
            "symbol": clean_symbol,
            "side": side.upper(),
            "quantity": float(quantity) if prefix == "us" else int(quantity),
            "price": float(price),
            "currency": currency
        }
        start_time = time.time()
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            duration_ms = (time.time() - start_time) * 1000.0
            try:
                db.log_api_call(
                    endpoint=f"/api/lsx/{prefix}/order/calculate",
                    method="POST",
                    params=payload,
                    status_code=resp.status_code,
                    response=resp.text[:500],
                    duration_ms=duration_ms
                )
            except Exception as e_db:
                logger.debug(f"Failed to log calculate_order API call: {e_db}")
                
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to calculate Bamboo order for {clean_symbol}: {e}")
            raise

    def place_order(self, order_payload: dict, symbol: str = None) -> dict:
        prefix = self._get_api_prefix(symbol=symbol)
        url = f"{self.base_url}/api/lsx/{prefix}/order"
        currency = "USD" if prefix == "us" else "NGN"
        headers = self._get_headers()
        headers["currency"] = currency
        payload = dict(order_payload)
        payload["currency"] = currency
        start_time = time.time()
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            duration_ms = (time.time() - start_time) * 1000.0
            try:
                db.log_api_call(
                    endpoint=f"/api/lsx/{prefix}/order",
                    method="POST",
                    params=payload,
                    status_code=resp.status_code,
                    response=resp.text[:500],
                    duration_ms=duration_ms
                )
            except Exception as e_db:
                logger.debug(f"Failed to log place_order API call: {e_db}")
                
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to place Bamboo order: {e}")
            raise

    def cancel_order(self, order_id: str, symbol: str = None) -> dict:
        prefix = self._get_api_prefix(symbol=symbol)
        url = f"{self.base_url}/api/lsx/{prefix}/order/{order_id}/cancel"
        headers = self._get_headers()
        start_time = time.time()
        try:
            resp = requests.post(url, headers=headers, timeout=10)
            duration_ms = (time.time() - start_time) * 1000.0
            try:
                db.log_api_call(
                    endpoint=f"/api/lsx/{prefix}/order/{order_id}/cancel",
                    method="POST",
                    params={},
                    status_code=resp.status_code,
                    response=resp.text[:500],
                    duration_ms=duration_ms
                )
            except Exception as e_db:
                logger.debug(f"Failed to log cancel_order API call: {e_db}")
                
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to cancel Bamboo order {order_id}: {e}")
            raise

    def get_order_status(self, order_id: str, symbol: str = None) -> dict:
        prefix = self._get_api_prefix(symbol=symbol)
        url = f"{self.base_url}/api/lsx/{prefix}/order/{order_id}/status"
        headers = self._get_headers()
        start_time = time.time()
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            duration_ms = (time.time() - start_time) * 1000.0
            try:
                db.log_api_call(
                    endpoint=f"/api/lsx/{prefix}/order/{order_id}/status",
                    method="GET",
                    params={},
                    status_code=resp.status_code,
                    response=resp.text[:500],
                    duration_ms=duration_ms
                )
            except Exception as e_db:
                logger.debug(f"Failed to log get_order_status API call: {e_db}")
                
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to check Bamboo order {order_id} status: {e}")
            raise

bamboo_client = BambooClient()
