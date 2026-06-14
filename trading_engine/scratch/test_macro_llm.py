import sys
import os
from pathlib import Path

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine.agents.macro_agent import _llm_macro
from trading_engine.utils.llm import call_llm

def test_macro():
    print("Testing macro LLM call...")
    try:
        # Let's run _llm_macro directly and print results
        sig, conf, reason = _llm_macro(
            dxy_trend="neutral",
            risk_mode="neutral",
            symbol="BTC/USDT",
            asset_type="crypto",
            current_price=63812.10,
            ma_50=62000.0,
            ma_200=60000.0
        )
        print(f"Success! Signal: {sig}, Confidence: {conf}, Reason: {reason}")
    except Exception as e:
        print(f"Failed: {e}")
        # Let's see what the LLM returns for the prompt
        prompt_path = Path(__file__).parent.parent / "prompts" / "macro_prompt.md"
        template = prompt_path.read_text()
        escaped = template.replace("{", "{{").replace("}", "}}")
        for placeholder in ["symbol", "asset_type", "dxy_trend", "risk_mode", "current_price", "ma_50", "ma_200"]:
            escaped = escaped.replace(f"{{{{{placeholder}}}}}", f"{{{placeholder}}}")
        prompt = escaped.format(
            symbol="BTC/USDT", asset_type="crypto",
            dxy_trend="neutral", risk_mode="neutral",
            current_price=63812.10, ma_50=62000.0, ma_200=60000.0
        )
        print("\n--- Raw Prompt ---")
        print(prompt)
        print("\n--- Raw LLM Response ---")
        try:
            resp = call_llm(prompt)
            print(resp)
            print(f"Type of response: {type(resp)}")
        except Exception as ex:
            print(f"LLM call itself failed: {ex}")

if __name__ == "__main__":
    test_macro()
