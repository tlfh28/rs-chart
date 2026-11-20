"""
코스피·코스닥 전 종목 RS(상대강도) 계산기
- IBD식 RS 점수(1~99): 최근 3·6·9·12개월 수익률을 2:1:1:1로 가중 → 전 종목 백분위
- RS 라인: 종가 ÷ 소속 시장 지수(코스피/코스닥)
결과는 docs/data/ 에 JSON으로 저장되고, docs/ 의 웹페이지가 이를 읽어 보여줍니다.

데이터: 종목 목록·업종 = KIND(한국거래소 상장공시시스템), 시가총액 = 네이버 금융,
        일봉·지수 = 네이버 금융 차트
"""
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data")
HIST_DAYS = 600          # 받아올 일봉 개수 (RS 점수 1년치 추이를 그리려면 약 2년 필요)
RS_HISTORY = 260         # RS 점수 추이를 저장할 거래일 수
MIN_DAYS = 63            # 상장 3개월 미만은 RS 점수 계산 제외
WORKERS = 8
KST = timezone(timedelta(hours=9))

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
})


def get(url, retries=3, **kw):
    for i in range(retries):
        try:
            r = S.get(url, timeout=15, **kw)
            r.raise_for_status()
            return r
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


# ───────────────────────── 1. 종목 목록 ─────────────────────────
def kind_list(market_type):
    """KIND 상장법인 목록 (ETF·ETN·우선주 없이 보통주 회사만, 업종 포함)"""
    url = ("https://kind.krx.co.kr/corpgeneral/corpList.do"
           f"?method=download&searchType=13&marketType={market_type}")
    r = get(url)
    html = r.content.decode("euc-kr", errors="replace")
    df = pd.read_html(io.StringIO(html), header=0)[0]
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
    return df[["회사명", "종목코드", "업종"]]


def naver_market_sum(sosok):
    """네이버 시가총액 순위 페이지 → {code: (name, 시가총액(억))}"""
    out = {}
    page = 1
    while True:
        url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
        html = get(url).content.decode("euc-kr", errors="replace")
        codes = re.findall(r'<a href="/item/main\.naver\?code=(\d{6})" class="tltle">', html)
        if not codes:
            break
        df = pd.read_html(io.StringIO(html), match="종목명")[0]
        df = df.dropna(subset=["종목명"])
        df = df[df["종목명"] != "종목명"].reset_index(drop=True)
        for i, code in enumerate(codes):
            if i >= len(df):
                break
            mcap = pd.to_numeric(str(df.loc[i, "시가총액"]).replace(",", ""), errors="coerce")
            out[code] = (str(df.loc[i, "종목명"]).strip(),
                         None if pd.isna(mcap) else int(mcap))
        page += 1
        if page > 60:
            break
    return out


ETF_PAT = re.compile(r"^(KODEX|TIGER|KBSTAR|RISE|ACE|SOL|HANARO|ARIRANG|KOSEF|PLUS|TIMEFOLIO|"
                     r"KIWOOM|WON|BNK|파워|마이티|히어로즈|에셋플러스|TRUSTON|1Q|UNICORN|DAISHIN343|VITA|"
                     r"FOCUS|마이다스|HK|KCGI|더제이)|ETN|스팩|선물|인버스|레버리지")


def build_universe():
    rows = []
    try:
        mcap_k = naver_market_sum(0)
        mcap_q = naver_market_sum(1)
    except Exception as e:
        print("네이버 시가총액 수집 실패:", e)
        mcap_k, mcap_q = {}, {}
    try:
        for mt, mk, mc in (("stockMkt", "KOSPI", mcap_k), ("kosdaqMkt", "KOSDAQ", mcap_q)):
            df = kind_list(mt)
            for _, r in df.iterrows():
                if "스팩" in r["회사명"]:
                    continue
                code = r["종목코드"]
                rows.append(dict(code=code, name=r["회사명"], market=mk,
                                 sector=str(r["업종"]) if pd.notna(r["업종"]) else "",
                                 mcap=mc.get(code, (None, None))[1]))
        print(f"KIND 종목 목록: {len(rows)}개")
    except Exception as e:
        print("KIND 목록 실패 → 네이버 목록으로 대체:", e)
        rows = []
        for mk, mc in (("KOSPI", mcap_k), ("KOSDAQ", mcap_q)):
            for code, (name, mcap) in mc.items():
                if ETF_PAT.search(name) or not code.endswith("0"):
                    continue  # ETF·ETN·스팩·우선주 제외
                rows.append(dict(code=code, name=name, market=mk, sector="", mcap=mcap))
        print(f"네이버 종목 목록: {len(rows)}개")
    if not rows:
        raise SystemExit("종목 목록을 가져오지 못했습니다.")
    return rows


# ───────────────────────── 2. 일봉 ─────────────────────────
def naver_daily(symbol, count=HIST_DAYS):
    url = (f"https://fchart.stock.naver.com/sise.nhn?symbol={symbol}"
           f"&timeframe=day&count={count}&requestType=0")
    r = get(url)
    root = ET.fromstring(r.content.decode("euc-kr", errors="replace"))
    recs = []
    for it in root.iter("item"):
        p = it.get("data").split("|")
        if len(p) < 6:
            continue
        recs.append((p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
    df = pd.DataFrame(recs, columns=["date", "o", "h", "l", "c", "v"])
    df = df[df["c"] > 0]
    return df.set_index("date")


def fetch_all(codes):
    data, fail = {}, []
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(naver_daily, c): c for c in codes}
        for i, f in enumerate(as_completed(futs), 1):
            c = futs[f]
            try:
                df = f.result()
                if len(df):
                    data[c] = df
            except Exception:
                fail.append(c)
            if i % 250 == 0:
                print(f"  일봉 {i}/{len(codes)}")
    print(f"일봉 수집 완료: {len(data)}개, 실패 {len(fail)}개")
    return data


# ───────────────────────── 3. RS 계산 ─────────────────────────
def compute(universe, prices, idx):
    cal = idx["KOSPI"].index.union(idx["KOSDAQ"].index).sort_values()
    codes = [u["code"] for u in universe if u["code"] in prices]
    close = pd.DataFrame({c: prices[c]["c"] for c in codes}).reindex(cal)
    listed = close.notna().cumsum()          # 상장 후 거래일 수
    close_f = close.ffill()

    def ret(n):
        past = close_f.shift(n).where(listed > n)   # 상장 기간이 n일보다 짧으면 빈 값
        return close_f / past - 1

    r63, r126, r189, r252 = ret(63), ret(126), ret(189), ret(252)
    # 상장 기간이 짧은 경우: 없는 구간은 가장 긴 가용 수익률로 대체
    r126 = r126.fillna(r63)
    r189 = r189.fillna(r126)
    r252 = r252.fillna(r189)
    raw = 2 * r63 + r126 + r189 + r252
    raw = raw.where(listed >= MIN_DAYS)
    # 거래정지(당일 거래 없음) 종목도 직전 값 유지
    pct = raw.rank(axis=1, pct=True)
    rs = (pct * 98 + 1).round()

    return cal, close, rs


def main():
    t0 = time.time()
    os.makedirs(os.path.join(OUT_DIR, "s"), exist_ok=True)
    universe = build_universe()

    print("지수 일봉 수집…")
    idx = {m: naver_daily(m) for m in ("KOSPI", "KOSDAQ")}

    print("종목 일봉 수집…")
    prices = fetch_all([u["code"] for u in universe])
    universe = [u for u in universe if u["code"] in prices]

    cal, close, rs = compute(universe, prices, idx)
    as_of = cal[-1]
    hist_cal = cal[-RS_HISTORY:]

    summary = []
    for u in universe:
        c = u["code"]
        p = prices[c].reindex(cal)
        if p["c"].isna().all():
            continue
        ix = idx[u["market"]]["c"].reindex(cal).ffill()
        cf = p["c"].ffill()
        rsl = cf / ix
        valid = p["c"].notna()
        dates = [d for d in cal[valid.values]][-HIST_DAYS:]
        sub = p.loc[dates]
        rsl_sub = rsl.loc[dates]
        base = rsl_sub.iloc[0]
        rs_ser = rs[c]

        # RS 라인/주가 52주(250거래일) 신고가 여부
        last250 = dates[-250:]
        rsl_nh = bool(rsl.loc[last250].iloc[-1] >= rsl.loc[last250].max() * 0.9999)
        hi250 = sub["h"].loc[last250].max()
        last_c = round(float(sub["c"].iloc[-1]), 2)
        price_nh = bool(last_c >= hi250 * 0.9999)
        from_hi = round((last_c / hi250 - 1) * 100, 1)

        def rs_at(k):
            v = rs_ser.iloc[-1 - k] if len(rs_ser) > k else np.nan
            return None if pd.isna(v) else int(v)

        prev_c = float(sub["c"].iloc[-2]) if len(sub) > 1 else last_c
        summary.append([
            c, u["name"], u["market"], u["sector"], u["mcap"], last_c,
            round((last_c / prev_c - 1) * 100, 2),
            rs_at(0), rs_at(5), rs_at(21), rs_at(63),
            int(rsl_nh), int(rsl_nh and not price_nh), from_hi,
            dates[-1] != as_of,     # 거래정지 등으로 최근일 데이터 없음
        ])

        # RS 점수 추이는 12개월 수익률을 온전히 계산할 수 있는 최근 RS_HISTORY일만 표시
        rs_hist = rs_ser.reindex(dates).where(pd.Index(dates).isin(hist_cal))
        detail = {
            "d": [int(x) for x in dates],
            "o": sub["o"].round(2).tolist(), "h": sub["h"].round(2).tolist(),
            "l": sub["l"].round(2).tolist(), "c": sub["c"].round(2).tolist(),
            "v": sub["v"].astype(int).tolist(),
            "rsl": (rsl_sub / base * 100).round(2).tolist(),
            "rs": [None if pd.isna(x) else int(x) for x in rs_hist.tolist()],
        }
        with open(os.path.join(OUT_DIR, "s", f"{c}.json"), "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, separators=(",", ":"))

    meta = {
        "as_of": as_of,
        "built_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "count": len(summary),
        "cols": ["code", "name", "market", "sector", "mcap", "close", "chg", "rs",
                 "rs_1w", "rs_1m", "rs_3m", "rsl_nh", "rsl_lead", "from_hi", "stale"],
    }
    # 지수도 저장 (차트 비교용)
    for m in ("KOSPI", "KOSDAQ"):
        d = idx[m].iloc[-HIST_DAYS:]
        with open(os.path.join(OUT_DIR, f"idx_{m}.json"), "w") as f:
            json.dump({"d": [int(x) for x in d.index], "c": d["c"].round(2).tolist()}, f,
                      separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "rows": summary}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"완료: {len(summary)}개 종목, 기준일 {as_of}, {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
