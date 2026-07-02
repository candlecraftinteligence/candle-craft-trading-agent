from __future__ import annotations

from datetime import datetime

from .models import NewsItem, ScoredItem, utc_now
from .narrative_classifier import (
    BTC_LIQUIDITY,
    ETH_ECOSYSTEM,
    ETF_FLOWS,
    HACK_EXPLOIT_SECURITY,
    LIQUIDATION_SQUEEZE,
    MACRO_FED_CPI_RATES,
    REGULATION_SEC_CFTC_EU_MICA,
    RUMOR,
    SOLANA_ECOSYSTEM,
    STABLECOIN_LIQUIDITY,
    WHALE_MOVEMENT,
)

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
PROMPT_MAX_LENGTH = 3800


def build_chatgpt_prompt(scored: ScoredItem) -> str:
    item = scored.news_item
    why = why_this_could_perform(scored)
    market_angle = market_angle_for(scored)
    visual_angle = suggested_visual_angle_for(scored)

    prompt = f"""Act as the Candle Craft Intelligence X content writer.

Create a short X content package from this crypto news.

Headline:
{_truncate(item.title, 240)}

Source:
{_truncate(item.source_name, 80)} - {_truncate(item.url, 300)}

Category:
{scored.category}

Why it matters:
{_truncate(why, 180)}

Market angle:
{_truncate(market_angle, 220)}

Visual angle:
{_truncate(visual_angle, 220)}

Rules:
- Write for crypto traders.
- Do not write a news article.
- Do not repeat the full headline unless necessary.
- Create sharp, compact X posts.
- Each X post must be maximum 240 characters total, including hashtags and brand ending.
- The 240-character limit also includes body text and line breaks.
- Use a sharp hook and compact market interpretation.
- Maximum 4 short lines.
- Avoid long sentences.
- Avoid em dashes if they make the post too long.
- Use exactly 3 relevant hashtags by default.
- Use 4 hashtags only if the post stays under 240 characters.
- Hashtags go at the end.
- Do not use hashtag spam.
- Do not use more than 4 hashtags.
- Add a character count after each post in parentheses, for example: "Character count: 218".
- If the post would exceed 240 characters, shorten it before returning.
- Use one compact brand ending:
  "Candle Craft Intelligence"
  or "CCI | Signal. Structure. Execution."
  or "The wolf tracks liquidity."
- Do not use the full long brand signature if it makes the post exceed 240 characters.
- No financial advice.
- No buy/sell instruction.
- No guaranteed profit claims.
- Do not invent facts beyond the headline and source.
- Do not present rumors as confirmed facts.

Hashtag choices:
- BTC / liquidity: #Bitcoin #BTC #Liquidity
- ETF flow: #BitcoinETF #BTC #CryptoMarkets
- ETH: #Ethereum #ETH #DeFi
- Solana: #Solana #SOL #Altcoins
- Security / hack: #CryptoSecurity #DeFi #CryptoNews
- Regulation: #CryptoRegulation #SEC #CryptoNews
- Macro: #Bitcoin #Macro #CryptoMarkets
- Liquidation / funding: #Bitcoin #Liquidations #CryptoTrading
- If the story is not about Bitcoin, do not force Bitcoin hashtags.

Image-generation prompt rules:
- Return one professional image-generation prompt the user can paste into an image tool.
- Design it for an X post image.
- Prefer 16:9 landscape.
- Use Candle Craft premium dark/orange/gold style.
- Match the category and narrative.
- Avoid fake screenshots.
- Avoid fake charts with specific made-up price levels.
- Avoid third-party logos unless legally safe and clearly necessary.
- Avoid small unreadable text.
- Avoid misleading claims.
- Use minimal generic text only if needed, such as "Liquidity Shift", "ETF Flow Watch", "Security Risk", "Macro Pressure", or "Market Structure Alert".

Alt text rules:
- Return alt text in 1 to 2 short sentences.
- Clearly describe the image.
- Include no hashtags.
- Include no financial advice.
- Avoid hype.

Return exactly:

1. Short X post
<post>
Character count: <number>

2. More aggressive engagement version
<post>
Character count: <number>

3. Image-generation prompt
<16:9 image prompt>

4. Alt text
<1 to 2 sentence alt text>

5. Optional thread angle
<thread angle or "No thread needed.">
"""
    return _truncate(prompt, PROMPT_MAX_LENGTH)

def build_telegram_message(scored: ScoredItem, prompt_text: str, *, now: datetime | None = None) -> str:
    item = scored.news_item
    message = f"""🟠 CCI X PROMPT

Score: {scored.final_score}/100 | {scored.category} | {_truncate(item.source_name, 80)}

Copy into ChatGPT:

{prompt_text}"""
    if len(message) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return message

    compact = f"""🟠 CCI X PROMPT

Score: {scored.final_score}/100 | {scored.category} | {_truncate(item.source_name, 80)}

Copy into ChatGPT:

{prompt_text}"""
    if len(compact) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return compact
    return _truncate(compact, TELEGRAM_MAX_MESSAGE_LENGTH)

def why_this_could_perform(scored: ScoredItem) -> str:
    narratives = set(scored.narratives)
    reasons: list[str] = []
    if ETF_FLOWS in narratives:
        reasons.append("ETF flow stories create immediate debate around institutional demand and spot market liquidity.")
    if BTC_LIQUIDITY in narratives:
        reasons.append("BTC liquidity headlines travel well because traders read them as market-wide risk signals.")
    if HACK_EXPLOIT_SECURITY in narratives:
        reasons.append("Security incidents create fear, urgency, and clear trader disagreement over contagion risk.")
    if LIQUIDATION_SQUEEZE in narratives:
        reasons.append("Liquidation and squeeze headlines are highly shareable because positioning is directly involved.")
    if REGULATION_SEC_CFTC_EU_MICA in narratives:
        reasons.append("Regulatory pressure tends to trigger strong comments from both risk-on and risk-off traders.")
    if WHALE_MOVEMENT in narratives:
        reasons.append("Whale movement gives traders a clean crowd-psychology angle without needing a price prediction.")
    if SOLANA_ECOSYSTEM in narratives:
        reasons.append("Solana rotation is narrative-driven and often pulls fast engagement from ecosystem traders.")
    if not reasons:
        reasons.append("The story has a clearer market narrative than generic crypto headlines.")
    return " ".join(reasons[:2])


def market_angle_for(scored: ScoredItem) -> str:
    narratives = set(scored.narratives)
    if ETF_FLOWS in narratives:
        return "Frame this as a liquidity-flow question: are institutions adding demand, removing demand, or changing trader expectations?"
    if HACK_EXPLOIT_SECURITY in narratives:
        return "Frame this around risk, trust, and whether traders should price in contagion or isolated damage."
    if LIQUIDATION_SQUEEZE in narratives:
        return "Frame this around crowded positioning, forced flows, and how liquidity hunts traders on both sides."
    if MACRO_FED_CPI_RATES in narratives:
        return "Frame this around macro liquidity and how rates expectations change crypto risk appetite."
    if STABLECOIN_LIQUIDITY in narratives:
        return "Frame this around stablecoin supply as dry powder and the liquidity backdrop behind market moves."
    if WHALE_MOVEMENT in narratives:
        return "Frame this around whether large wallets are reacting to liquidity or trying to shape crowd psychology."
    if RUMOR in narratives:
        return "Frame this cautiously as an unconfirmed market narrative, not as settled fact."
    return "Frame this around liquidity, market structure, and trader psychology rather than a simple news recap."


def hook_direction_for(scored: ScoredItem) -> str:
    narratives = set(scored.narratives)
    if ETF_FLOWS in narratives:
        return "Lead with the flow number or institutional name, then ask what it says about real demand."
    if HACK_EXPLOIT_SECURITY in narratives:
        return "Lead with the risk event, then connect it to trust, liquidity flight, and trader behavior."
    if LIQUIDATION_SQUEEZE in narratives:
        return "Lead with forced positioning and make the hook about who got trapped."
    if REGULATION_SEC_CFTC_EU_MICA in narratives:
        return "Lead with the legal shock and why it changes the market's risk calculation."
    if BTC_LIQUIDITY in narratives:
        return "Lead with BTC as the liquidity compass for the rest of the market."
    return "Lead with the part most traders would miss, then connect it to liquidity or positioning."


def suggested_visual_angle_for(scored: ScoredItem) -> str:
    narratives = set(scored.narratives)
    if scored.category == "ETF_FLOW" or ETF_FLOWS in narratives:
        return "Institutional liquidity flow graphic with BTC/ETH, ETF-style inflow arrows, dark premium trading desk atmosphere."
    if scored.category == "SECURITY" or HACK_EXPLOIT_SECURITY in narratives:
        return "Dark blockchain security alert image with shield, exploit-warning energy, and orange risk lighting."
    if scored.category == "LIQUIDATION" or LIQUIDATION_SQUEEZE in narratives:
        return "Liquidity cascade/orderbook pressure visual with clean market-structure style."
    if scored.category == "REGULATION" or REGULATION_SEC_CFTC_EU_MICA in narratives:
        return "Regulatory pressure visual with courthouse/blockchain theme, premium dark fintech style."
    if scored.category == "MACRO" or MACRO_FED_CPI_RATES in narratives:
        return "Macro pressure visual with Fed/rate/dollar atmosphere and crypto market overlay."
    if scored.category == "BTC_LIQUIDITY" or BTC_LIQUIDITY in narratives:
        return "Bitcoin liquidity map visual with orderbook depth, orange/gold highlights, and wolf-market energy."
    if scored.category == "SOLANA" or SOLANA_ECOSYSTEM in narratives:
        return "Solana ecosystem rotation visual with fast liquidity arrows, premium dark market dashboard energy."
    if scored.category == "ETH" or ETH_ECOSYSTEM in narratives:
        return "Ethereum market-structure visual with DeFi liquidity layers, orange/gold highlights, and clean fintech style."
    return "Premium dark crypto market-structure graphic with orange/gold liquidity highlights and clean Candle Craft visual energy."


def format_freshness(item: NewsItem, now: datetime | None = None) -> str:
    age = item.age_hours(now or utc_now())
    if age is None:
        return "N/A"
    if age < 1:
        return f"{max(1, round(age * 60))} minutes old"
    if age < 24:
        return f"{round(age, 1)} hours old"
    return f"{round(age / 24, 1)} days old"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 20:
        return text[:limit]
    return f"{text[: limit - 16].rstrip()}\n...[shortened]"













