"""
trading_engine/utils/llm.py
Centralized LLM client with local smart mock/fallback capability.
Allows running the trading engine without OpenAI or Anthropic API keys.
"""
from __future__ import annotations
import json
import re
from loguru import logger
from trading_engine.config import settings

def _clean_markdown_json(text: str) -> str:
    """Strips markdown block markers like ```json ... ``` from the text."""
    text = text.strip()
    
    # Remove leading markdown block marker
    if text.startswith("```"):
        newline_idx = text.find("\n")
        if newline_idx != -1:
            text = text[newline_idx:].strip()
        else:
            text = re.sub(r"^```(?:json)?", "", text).strip()
            
    # Remove trailing markdown block marker if present
    if text.endswith("```"):
        text = text[:-3].strip()
        
    return text

def call_llm(prompt: str, system_prompt: str = "") -> str:
    """
    Calls the configured LLM provider (OpenAI, Anthropic, or OpenRouter).
    If api keys are missing or provider is set to 'mock', runs a local smart mock analyzer.
    """
    provider = settings.llm_provider.lower()
    
    # Check if we should use the actual API
    has_openai = bool(settings.openai_api_key)
    has_anthropic = bool(settings.anthropic_api_key)
    has_openrouter = bool(settings.openrouter_api_key)
    
    use_real = False
    if provider == "openai" and has_openai:
        use_real = True
    elif provider == "anthropic" and has_anthropic:
        use_real = True
    elif provider == "openrouter" and has_openrouter:
        use_real = True
        
    if use_real:
        try:
            if provider == "openai":
                from openai import OpenAI
                client = OpenAI(api_key=settings.openai_api_key)
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": prompt})
                
                response = client.chat.completions.create(
                    model=settings.llm_model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=200,
                )
                raw_text = response.choices[0].message.content.strip()
                return _clean_markdown_json(raw_text)
            elif provider == "openrouter":
                from openai import OpenAI
                client = OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=settings.openrouter_api_key,
                )
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": prompt})
                
                response = client.chat.completions.create(
                    model=settings.llm_model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=200,
                    extra_headers={
                        "HTTP-Referer": "https://github.com/google-deepmind/antigravity",
                        "X-Title": "Antigravity Trading System",
                    }
                )
                raw_text = response.choices[0].message.content.strip()
                return _clean_markdown_json(raw_text)
            elif provider == "anthropic":
                import anthropic
                client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
                system_arg = {"system": system_prompt} if system_prompt else {}
                response = client.messages.create(
                    model=settings.llm_model,
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}],
                    **system_arg
                )
                raw_text = response.content[0].text.strip()
                return _clean_markdown_json(raw_text)
        except Exception as e:
            logger.warning(f"Real LLM call failed ({provider}): {e}. Falling back to local mock analyzer.")
            
    # Run the smart local mock analyzer
    return _smart_mock_llm(prompt, system_prompt)

def _smart_mock_llm(prompt: str, system_prompt: str) -> str:
    """
    Simulates the LLM by parsing the prompt structure and returning
    logical, domain-specific responses.
    """
    # ── 1. Sentiment Agent Analysis ──
    if "SentimentAgent" in prompt or "headlines" in prompt.lower():
        # Parse symbol
        symbol_match = re.search(r"analyzing ([A-Z0-9/]+)", prompt)
        symbol = symbol_match.group(1) if symbol_match else "Asset"
        
        # Extract headlines
        headlines = []
        for line in prompt.split("\n"):
            line_str = line.strip()
            if line_str.startswith("- "):
                headlines.append(line_str[2:])
                
        # Analyze headlines via simple keyword heuristic
        pos_words = ["bullish", "rally", "adopt", "buy", "up", "green", "gain", "partnership", "approve", "launch", "record", "growth", "inflow"]
        neg_words = ["hack", "crash", "crackdown", "sec", "lawsuit", "bearish", "drop", "plunge", "down", "red", "investigation", "fined", "sell", "outflow", "ban"]
        
        pos_count = 0
        neg_count = 0
        for h in headlines:
            h_lower = h.lower()
            pos_count += sum(1 for w in pos_words if w in h_lower)
            neg_count += sum(1 for w in neg_words if w in h_lower)
            
        if pos_count > neg_count:
            signal = "BUY"
            confidence = min(95.0, 60.0 + (pos_count - neg_count) * 10)
            reason = f"Bullish news headlines indicate positive sentiment and potential upward momentum for {symbol}."
        elif neg_count > pos_count:
            signal = "SELL"
            confidence = min(95.0, 60.0 + (neg_count - pos_count) * 10)
            reason = f"Bearish news headlines indicate negative sentiment and regulatory/downward pressure for {symbol}."
        else:
            signal = "HOLD"
            confidence = 50.0
            reason = f"Mixed or neutral headlines for {symbol} suggest a balanced market sentiment."
            
        return json.dumps({
            "signal": signal,
            "confidence": confidence,
            "reason": reason
        })
        
    # ── 2. Macro Agent Analysis ──
    if "MacroAgent" in prompt or "macro data" in prompt.lower():
        # Extract DXY and Risk Mode
        dxy_match = re.search(r"DXY \(dollar\) trend:\s*(\w+)", prompt, re.IGNORECASE)
        risk_match = re.search(r"Market risk mode:\s*([\w\-]+)", prompt, re.IGNORECASE)
        symbol_match = re.search(r"analyzing ([A-Z0-9/]+)", prompt)
        
        dxy = dxy_match.group(1).lower() if dxy_match else "neutral"
        risk = risk_match.group(1).lower() if risk_match else "neutral"
        symbol = symbol_match.group(1) if symbol_match else "Asset"
        
        if dxy == "rising" or risk == "risk-off":
            signal = "SELL"
            confidence = 70.0
            reason = f"Macro headwinds for {symbol} due to rising DXY (dollar strength) and risk-off market regime."
        elif dxy == "falling" and risk == "risk-on":
            signal = "BUY"
            confidence = 80.0
            reason = f"Macro tailwinds for {symbol} driven by falling DXY and a risk-on market environment."
        else:
            signal = "HOLD"
            confidence = 50.0
            reason = f"Macro variables are neutral or conflicting for {symbol}."
            
        return json.dumps({
            "signal": signal,
            "confidence": confidence,
            "reason": reason
        })
        
    # ── 3. Judge Agent Analysis ──
    if "JudgeAgent" in prompt or "agent reports" in prompt.lower():
        # Parse decision and confidence
        decision_match = re.search(r"Final decision:\s*(\w+)", prompt)
        confidence_match = re.search(r"with\s*([\d\.]+)%", prompt)
        
        decision = decision_match.group(1) if decision_match else "HOLD"
        confidence = confidence_match.group(1) if confidence_match else "50"
        
        # Try to parse agent signals to make a better reason
        agree_agents = []
        disagree_agents = []
        try:
            # Look for JSON block in prompt
            json_block_match = re.search(r"Agent reports:\s*(\[.*?\])", prompt, re.DOTALL)
            if json_block_match:
                reports = json.loads(json_block_match.group(1))
                for rep in reports:
                    if rep.get("signal") == decision:
                        agree_agents.append(rep.get("agent"))
                    else:
                        disagree_agents.append(rep.get("agent"))
        except Exception:
            pass
            
        if decision == "BUY":
            agree_str = f" agreeing ({', '.join(agree_agents)})" if agree_agents else ""
            disagree_str = f" despite minor disagreements from {', '.join(disagree_agents)}" if disagree_agents else ""
            return f"The weighted ensemble resolved to a BUY decision at {confidence}% confidence{agree_str}, indicating robust confluence across multiple directional models{disagree_str}."
        elif decision == "SELL":
            agree_str = f" agreeing ({', '.join(agree_agents)})" if agree_agents else ""
            disagree_str = f" despite minor disagreements from {', '.join(disagree_agents)}" if disagree_agents else ""
            return f"The weighted ensemble resolved to a SELL decision at {confidence}% confidence{agree_str}, highlighting bearish indicators alignment{disagree_str}."
        else:
            return f"The ensemble vote resolved to a HOLD decision at {confidence}% confidence as the system did not reach the threshold consensus or directional agents are conflicting."

    # Default fallback
    return "Neutral signal with standard market parameters."
