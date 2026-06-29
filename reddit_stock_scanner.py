"""
Reddit Stock Scanner - 매일 아침 실행
Reddit에서 주식 언급량/감성 수집 → Claude API 분석 → Telegram 발송
"""

import os
import re
import time
import requests
from datetime import datetime, timezone
from collections import defaultdict

# ── 환경 변수 (GitHub Actions Secrets 또는 Colab userdata) ──────────────────
REDDIT_CLIENT_ID     = os.environ["REDDIT_CLIENT_ID"]
REDDIT_CLIENT_SECRET = os.environ["REDDIT_CLIENT_SECRET"]
REDDIT_USER_AGENT    = os.environ.get("REDDIT_USER_AGENT", "StockScanner/1.0")
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID     = os.environ["TELEGRAM_CHAT_ID"]

# ── 설정 ──────────────────────────────────────────────────────────────────────
SUBREDDITS    = ["wallstreetbets", "stocks", "investing", "StockMarket"]
TOP_N         = 20        # 상위 N개 종목만 Claude에 전달
MIN_MENTIONS  = 3         # 최소 언급 수 (노이즈 필터)
FETCH_LIMIT   = 100       # 서브레딧당 게시글 수
TIME_FILTER   = "day"     # "day" | "week"

# 일반 단어 필터 (오탐 방지)
EXCLUDE_WORDS = {
    "FOR", "ARE", "NOW", "ALL", "NEW", "USA", "CEO", "IPO", "ETF", "GDP",
    "CPI", "FED", "SEC", "FDA", "THE", "IRS", "AND", "NOT", "BUT", "YOU",
    "HAS", "ITS", "CAN", "GET", "PUT", "CALL", "EDIT", "UPDATE", "WAS",
    "THIS", "THAT", "FROM", "WITH", "HAVE", "WILL", "BEEN", "WHAT", "WHEN",
    "THEN", "THAN", "THEY", "JUST", "LONG", "SHORT", "YOLO", "GAIN", "LOSS",
    "HIGH", "OPEN", "DOWN", "HOLD", "SELL", "REAL", "GOOD", "NEXT", "WEEK",
    "YEAR", "LATE", "LAST", "BULL", "BEAR", "PUMP", "DUMP", "BACK", "INTO",
    "OVER", "MORE", "SOME", "ALSO", "VERY", "MOST", "ONLY", "EVEN", "BOTH",
    "SAME", "EACH", "SUCH", "DOES", "MUCH", "NEED", "KNOW", "LIKE", "WELL",
    "CASH", "DEBT", "LINK", "POST", "PAID", "RATE", "RISK", "PLAY", "MOVE",
}

TICKER_PATTERN = re.compile(r'\b([A-Z]{2,5})\b')


# ─────────────────────────────────────────────────────────────────────────────
# 1. Reddit 수집
# ─────────────────────────────────────────────────────────────────────────────

def get_reddit_token() -> str:
    resp = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": REDDIT_USER_AGENT},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_posts(token: str, subreddit: str, limit: int = FETCH_LIMIT) -> list[dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": REDDIT_USER_AGENT,
    }
    url = f"https://oauth.reddit.com/r/{subreddit}/hot"
    resp = requests.get(url, headers=headers, params={"limit": limit}, timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]["children"]


def extract_tickers(text: str) -> list[str]:
    return [t for t in TICKER_PATTERN.findall(text) if t not in EXCLUDE_WORDS]


def collect_mentions() -> dict[str, dict]:
    """
    반환 형식:
    {
      "NVDA": {
        "count": 42,
        "sources": ["wallstreetbets", "stocks"],
        "sample_titles": ["Why NVDA is going to ...", ...],
        "upvotes": 1500,
      }, ...
    }
    """
    token = get_reddit_token()
    ticker_data: dict[str, dict] = defaultdict(lambda: {
        "count": 0,
        "sources": set(),
        "sample_titles": [],
        "upvotes": 0,
    })

    for sub in SUBREDDITS:
        try:
            posts = fetch_posts(token, sub)
            for post in posts:
                data      = post["data"]
                title     = data.get("title", "")
                selftext  = data.get("selftext", "")
                upvotes   = data.get("score", 0)
                tickers   = extract_tickers(title + " " + selftext)

                for t in set(tickers):
                    ticker_data[t]["count"]   += tickers.count(t)
                    ticker_data[t]["sources"].add(sub)
                    ticker_data[t]["upvotes"] += upvotes
                    if len(ticker_data[t]["sample_titles"]) < 3:
                        ticker_data[t]["sample_titles"].append(title[:120])

            time.sleep(1)   # Reddit API rate limit 준수
        except Exception as e:
            print(f"[WARN] {sub} 수집 실패: {e}")

    # set → list 변환
    for v in ticker_data.values():
        v["sources"] = list(v["sources"])

    return dict(ticker_data)


def filter_and_rank(ticker_data: dict) -> list[dict]:
    filtered = [
        {"ticker": k, **v}
        for k, v in ticker_data.items()
        if v["count"] >= MIN_MENTIONS
    ]
    # 언급 수 × 서브레딧 수 × 업보트 가중치로 정렬
    filtered.sort(
        key=lambda x: x["count"] * len(x["sources"]) + x["upvotes"] // 100,
        reverse=True,
    )
    return filtered[:TOP_N]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Claude API 분석
# ─────────────────────────────────────────────────────────────────────────────

def build_prompt(ranked: list[dict]) -> str:
    lines = []
    for item in ranked:
        lines.append(
            f"- {item['ticker']}: 언급 {item['count']}회 | "
            f"서브레딧: {', '.join(item['sources'])} | "
            f"누적 업보트: {item['upvotes']} | "
            f"샘플 제목: {' / '.join(item['sample_titles'][:2])}"
        )
    data_block = "\n".join(lines)

    return f"""당신은 Reddit 주식 데이터 분석 전문가입니다.
아래는 오늘 Reddit에서 가장 많이 언급된 종목 목록입니다 (언급 수, 서브레딧, 업보트, 게시글 제목 샘플 포함).

[데이터]
{data_block}

[분석 지침]
1. 각 종목을 세 가지 기준으로 평가하세요:
   - 모멘텀 신호: 단순 밈/반복인지, 실질적 스토리(계약·실적·숏스퀴즈 등)가 있는지
   - 감성 방향: 상승 심리 / 하락 심리 / 혼재
   - 주목 이유: 제목 샘플에서 유추되는 핵심 촉매

2. 상위 5개 종목을 "오늘의 레이더"로 선정하고, 각각 2~3문장으로 요약

3. 경고 종목(밈/과대광고 의심)이 있으면 별도 표시

4. 마지막에 한 줄 총평

[출력 형식] - Telegram 메시지용 (이모지 사용, 마크다운 없이 일반 텍스트)
"""


def analyze_with_claude(ranked: list[dict]) -> str:
    prompt = build_prompt(ranked)
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Telegram 발송
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    now_kst = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    full_msg = f"📡 Reddit 주식 레이더 | {now_kst}\n{'─'*35}\n\n{message}"

    # Telegram 메시지 4096자 제한 처리
    chunks = [full_msg[i:i+4000] for i in range(0, len(full_msg), 4000)]
    for chunk in chunks:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
            timeout=10,
        )
        resp.raise_for_status()
        time.sleep(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("▶ Reddit 데이터 수집 중...")
    ticker_data = collect_mentions()

    print(f"  원시 종목 수: {len(ticker_data)}")
    ranked = filter_and_rank(ticker_data)
    print(f"  필터 후 분석 대상: {len(ranked)}개")

    if not ranked:
        send_telegram("⚠️ 오늘은 유의미한 언급 종목이 없습니다.")
        return

    print("▶ Claude 분석 중...")
    analysis = analyze_with_claude(ranked)

    print("▶ Telegram 발송 중...")
    send_telegram(analysis)
    print("✅ 완료")


if __name__ == "__main__":
    main()
