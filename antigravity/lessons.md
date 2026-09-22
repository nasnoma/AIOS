# Lessons Learned

## Polymarket CLOB v2 Signature & SDK Migration
- **Issue**: Attempting to place orders using `py-clob-client` returned: `PolyApiException[status_code=400, error_message={'error': 'invalid order version, please use the latest clob-client'}]`
- **Root Cause**: In early 2026, Polymarket deprecated older order schemas and signatures. The standard `py-clob-client` (v0.34.6) is no longer compatible.
- **Resolution**: Use the package `py-clob-client-v2` instead. It acts as a replacement, but uses the python import namespace `py_clob_client_v2`. Therefore, import statements must be updated to:
  ```python
  from py_clob_client_v2.client import ClobClient
  from py_clob_client_v2.clob_types import OrderArgs, OrderType
  ```

## Geoblocking & Proxy Country Selection
- **Issue**: Polymarket restricts trading in various regions, returning `403 Forbidden` errors on REST calls.
- **Allowed Locations**: Ireland (`IE`), Germany (`DE` - wait, Germany is blocked, so double-check), Turkey (`TR`), Norway (`NO`), Switzerland (`CH`), South Africa (`ZA`).
- **Blocked Locations**: United States (`US`), United Kingdom (`UK`), Singapore (`SG`), Spain (`ES`), Poland (`PL`), Japan (`JP`), France (`FR`), Italy (`IT`), Canada (`CA`).
- **Lesson**: Do not assume any country is allowed without checking against the latest Polymarket restricted list. Run a verification test against `https://clob.polymarket.com/book?token_id=...` using the proxy before deploying.

## Reporting Discipline & Engine Invariants
- **Never Conflate Fill P&L with Bag P&L**: A mark price trading above the latest grid fill price does NOT mean the position is green. The engine exits against the true **FIFO lot cost basis**. Always quote P&L relative to the actual bag cost basis, not isolated fill executions.
- **Code Reality over Narrative Fluff**: Never dress up algorithmic decisions in subjective market narratives (e.g. "institutional order blocks", "support sniping"). Speak strictly to the mathematical parameters in code: ATR %, ADX, 4H SMA200, and fee-proof formulas.
- **Process Guaranteed, Bounce Not**: Entry gates and exitability checks improve odds and filter high-risk environments, but they do NOT guarantee a bounce. Frame performance with humility: code guarantees process and downside protection; the market dictates the bounce.
- **No Unsolicited P&L Cheerleading / Discrepant Figures**: Answer strictly what was asked without tacking on unsolicited daily profit or cycle statistics. Never quote internal SQLite logs that diverge from what is displayed on the user's dashboard or in their Bybit account.
