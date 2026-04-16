from flask import Flask, jsonify
import httpx
import asyncio
from datetime import datetime

app = Flask(__name__)

BINANCE_FAPI = "https://fapi.binance.com"

cache = {}

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

async def get_binance_data():
    try:
        async with httpx.AsyncClient() as client:
            # 获取 symbols
            r = await client.get(
                f"{BINANCE_FAPI}/fapi/v1/exchangeInfo",
                timeout=20
            )
            data = r.json()
            symbols = [
                s["symbol"] for s in data.get("symbols", [])
                if s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
            ][:100]
            
            # 获取 24h ticker
            r = await client.get(
                f"{BINANCE_FAPI}/fapi/v1/ticker/24hr",
                timeout=20
            )
            ticker_map = {x["symbol"]: x for x in r.json()}
            
            # 获取 premium index
            r = await client.get(
                f"{BINANCE_FAPI}/fapi/v1/premiumIndex",
                timeout=20
            )
            premium_map = {x["symbol"]: x for x in r.json()}
            
            # 获取市值数据
            r = await client.get(
                "https://api.coingecko.com/api/v3/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                },
                timeout=20
            )
            markets = r.json()
            mcap_map = {}
            for m in markets:
                symbol = m.get("symbol", "").upper()
                if symbol:
                    mcap_map[f"{symbol}USDT"] = m.get("market_cap")
            
            rows = []
            for symbol in symbols:
                try:
                    ticker = ticker_map.get(symbol, {})
                    premium = premium_map.get(symbol, {})
                    
                    price = float(premium.get("markPrice") or ticker.get("lastPrice") or 0)
                    if price == 0:
                        continue
                    
                    oi = float(ticker.get("openInterest") or 0)
                    quote_vol = float(ticker.get("quoteVolume") or 0)
                    funding = float(premium.get("lastFundingRate") or 0) * 100
                    price_change = float(ticker.get("priceChangePercent") or 0)
                    oi_value = oi * price
                    
                    # 获取 OI 24h 变化
                    try:
                        r = await client.get(
                            f"{BINANCE_FAPI}/futures/data/openInterestHist",
                            params={"symbol": symbol, "period": "5m", "limit": 288},
                            timeout=10
                        )
                        hist = r.json()
                        oi_24h_pct = None
                        if len(hist) >= 2:
                            current = float(hist[-1]["sumOpenInterest"])
                            old = float(hist[0]["sumOpenInterest"])
                            if old > 0:
                                oi_24h_pct = (current - old) / old * 100
                    except:
                        oi_24h_pct = None
                    
                    mcap = mcap_map.get(symbol)
                    vol_mcap_pct = None
                    oi_value_mcap_pct = None
                    
                    if mcap and mcap > 0:
                        vol_mcap_pct = (quote_vol / mcap) * 100
                        oi_value_mcap_pct = (oi_value / mcap) * 100
                    
                    score = 50.0
                    if vol_mcap_pct:
                        score += min(vol_mcap_pct / 3, 15)
                    if oi_value_mcap_pct:
                        score += min(oi_value_mcap_pct / 3, 15)
                    if oi_24h_pct:
                        score += min(oi_24h_pct / 5, 20)
                    score += min(abs(funding) * 100, 15)
                    
                    tags = []
                    if oi_24h_pct and oi_24h_pct > 20:
                        tags.append("OI暴增")
                    if funding > 0.04:
                        tags.append("多头拥挤")
                    if vol_mcap_pct and vol_mcap_pct > 30:
                        tags.append("放量")
                    if oi_value_mcap_pct and oi_value_mcap_pct > 20:
                        tags.append("杠杆集中")
                    
                    rows.append({
                        "symbol": symbol,
                        "price": round(price, 8),
                        "market_cap": mcap,
                        "volume_mcap_pct": round(vol_mcap_pct, 2) if vol_mcap_pct else None,
                        "oi_value_mcap_pct": round(oi_value_mcap_pct, 2) if oi_value_mcap_pct else None,
                        "oi_24h_pct": round(oi_24h_pct, 2) if oi_24h_pct else None,
                        "funding_rate_pct": round(funding, 4),
                        "price_change_24h_pct": round(price_change, 2),
                        "quote_volume_24h": round(quote_vol, 0),
                        "open_interest": round(oi, 0),
                        "score": round(min(score, 100), 2),
                        "tags": tags,
                    })
                except:
                    continue
            
            rows = sorted(rows, key=lambda r: r.get("score", 0) or 0, reverse=True)
            
            fundings = [float(p.get("lastFundingRate", 0)) * 100 for p in premium_map.values()]
            avg_funding = sum(fundings) / len(fundings) if fundings else 0
            
            return {
                "table": rows,
                "stats": {
                    "total_symbols": len(ticker_map),
                    "avg_funding": round(avg_funding, 4),
                }
            }
    except Exception as e:
        print(f"Error: {e}")
        return {"table": [], "stats": {"total_symbols": 0, "avg_funding": 0}}

def get_event_loop():
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

@app.route('/api/table')
def get_table():
    try:
        loop = get_event_loop()
        result = loop.run_until_complete(get_binance_data())
        return jsonify({
            "code": 200,
            "data": result["table"],
            "total": len(result["table"]),
            "timestamp": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        return jsonify({"code": 500, "error": str(e), "data": []})

@app.route('/api/stats')
def get_stats():
    try:
        loop = get_event_loop()
        result = loop.run_until_complete(get_binance_data())
        return jsonify({
            "code": 200,
            "data": result["stats"],
        })
    except Exception as e:
        return jsonify({"code": 500, "error": str(e), "data": {}})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
