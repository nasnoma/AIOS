import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent.parent))

from trading_engine.utils.llm import call_llm
from trading_engine.config import settings

def test_or():
    print(f"Current LLM Provider: {settings.llm_provider}")
    print(f"Current LLM Model: {settings.llm_model}")
    print(f"OpenRouter API Key present: {bool(settings.openrouter_api_key)}")
    
    prompt = "Reply with exactly 'HELLO FROM OPENROUTER'"
    print("\nCalling LLM...")
    res = call_llm(prompt)
    print(f"Result: {res}")

if __name__ == "__main__":
    test_or()
