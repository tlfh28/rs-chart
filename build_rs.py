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
            if page == 1:
                print("  시세 페이지에서 종목을 찾지 못함:", len(html), html[:200].replace("\n", " "))
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


def naver_mcap_api(market):
    """네이버 모바일 API 시가총액 순위 → {code: (name, 시가총액(억), 종목유형)}"""
    out = {}
    for page in range(1, 40):
        url = (f"https://m.stock.naver.com/api/stocks/marketValue/{market}"
               f"?page={page}&pageSize=100")
        j = get(url).json()
        items = j.get("stocks") or []
        if page == 1 and items:
            print("  시총 API 예시:", {k: items[0].get(k) for k in
                                     ("itemCode", "stockName", "marketValue", "stockEndType")})
        if not items:
            break
        for it in items:
            mv = pd.to_numeric(str(it.get("marketValue", "")).replace(",", ""), errors="coerce")
            out[it["itemCode"]] = (it.get("stockName", ""), None if pd.isna(mv) else int(mv),
                                   it.get("stockEndType", ""))
    return out


ETF_PAT = re.compile(r"^(KODEX|TIGER|KBSTAR|RISE|ACE|SOL|HANARO|ARIRANG|KOSEF|PLUS|TIMEFOLIO|"
                     r"KIWOOM|WON|BNK|파워|마이티|히어로즈|에셋플러스|TRUSTON|1Q|UNICORN|DAISHIN343|VITA|"
                     r"FOCUS|마이다스|HK|KCGI|더제이)|ETN|스팩|선물|인버스|레버리지")


CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universe.json")


def build_universe():
    rows = []
    mcap_k, mcap_q = {}, {}
    try:
        mcap_k, mcap_q = naver_mcap_api("KOSPI"), naver_mcap_api("KOSDAQ")
        print(f"시총(API): 코스피 {len(mcap_k)}, 코스닥 {len(mcap_q)}")
    except Exception as e:
        print("시총 API 실패:", e)
    if not mcap_k:
        try:
            mcap_k, mcap_q = naver_market_sum(0), naver_market_sum(1)
            print(f"시총(시세 페이지): 코스피 {len(mcap_k)}, 코스닥 {len(mcap_q)}")
        except Exception as e:
            print("네이버 시가총액 수집 실패:", e)
    mc_all = {**mcap_k, **mcap_q}

    kind = None
    for attempt in range(4):
        try:
            kind = {mk: kind_list(mt) for mt, mk in (("stockMkt", "KOSPI"), ("kosdaqMkt", "KOSDAQ"))}
            break
        except Exception as e:
            print(f"KIND 목록 실패({attempt + 1}/4):", e)
            time.sleep(20)
    if kind:
        for mk, df in kind.items():
            for _, r in df.iterrows():
                if "스팩" in r["회사명"]:
                    continue
                code = r["종목코드"]
                rows.append(dict(code=code, name=r["회사명"], market=mk,
                                 sector=str(r["업종"]) if pd.notna(r["업종"]) else "",
                                 mcap=(mc_all.get(code) or (None, None))[1]))
        print(f"KIND 종목 목록: {len(rows)}개")
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump([{k: v for k, v in r.items() if k != "mcap"} for r in rows], f,
                      ensure_ascii=False, indent=0)
        return rows
    if os.path.exists(CACHE):
        base = json.load(open(CACHE, encoding="utf-8"))
        known = {r["code"] for r in base}
        for r in base:
            r["mcap"] = (mc_all.get(r["code"]) or (None, None))[1]
        # 저장본 이후 신규 상장 종목 추가 (네이버 목록 기준, 보통주만)
        for mk, mc in (("KOSPI", mcap_k), ("KOSDAQ", mcap_q)):
            for code, v in mc.items():
                if code in known or not code.endswith("0") or ETF_PAT.search(v[0]):
                    continue
                if len(v) > 2 and v[2] and v[2] != "stock":
                    continue
                base.append(dict(code=code, name=v[0], market=mk, sector="", mcap=v[1]))
        print(f"저장된 종목 목록 사용: {len(base)}개")
        return base
    if True:
        print("KIND·저장본 없음 → 네이버 목록으로 대체")
        for mk, mc in (("KOSPI", mcap_k), ("KOSDAQ", mcap_q)):
            for code, v in mc.items():
                name, mcap = v[0], v[1]
                if len(v) > 2 and v[2] and v[2] != "stock":
                    continue
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
    # 거래정지일 등 시가·고가·저가가 0으로 오는 경우 종가로 채움
    for k in ("o", "h", "l"):
        df.loc[df[k] <= 0, k] = df["c"]
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


# ───────────────────────── 4. 미너비니 스크리너 ─────────────────────────
TT_NAMES = ["종가 > 150·200일선", "150일선 > 200일선", "200일선 1개월 상승", "50일선 > 150·200일선",
            "종가 > 50일선", "52주 저점 대비 +30%↑", "52주 고점 대비 −25% 이내", "RS 70↑"]


def screen(sub, rs_now):
    """트렌드 템플릿(8개 조건)과 VCP(변동성 수축) 판정.
    sub: 상장 후 일봉(o,h,l,c,v), 날짜 오름차순."""
    out = dict(tt=0, ttm=0, vcp=0, dep=[], pivot=None, pvd=None, vdry=None, st="")
    c = sub["c"].to_numpy(float); h = sub["h"].to_numpy(float)
    l = sub["l"].to_numpy(float); v = sub["v"].to_numpy(float)
    n = len(c)
    if n < 60:
        return out
    last = c[-1]
    s = pd.Series(c)
    ma = {k: s.rolling(k).mean().to_numpy() for k in (50, 150, 200)}
    v50 = v[-50:].mean()
    out["vdry"] = round(float(v[-10:].mean() / v50), 2) if v50 > 0 else None

    # ① 트렌드 템플릿 — 이동평균이 계산 안 되는 신규 상장주는 해당 조건 불통과
    if n >= 222:
        hi52, lo52 = h[-250:].max(), l[-250:].min()
        m50, m150, m200 = ma[50][-1], ma[150][-1], ma[200][-1]
        checks = [last > m150 and last > m200, m150 > m200, m200 > ma[200][-22],
                  m50 > m150 and m50 > m200, last > m50, last >= lo52 * 1.30,
                  last >= hi52 * 0.75, (rs_now or 0) >= 70]
        out["tt"] = int(sum(checks))
        out["ttm"] = int(sum(1 << i for i, b in enumerate(checks) if b))

    # ② VCP — 최근 약 4개월(90거래일) 베이스 안의 고점→저점 하락폭이 점점 줄어드는지
    B, k = min(n, 90), 4
    hh, ll = h[-B:], l[-B:]
    piv = []
    for i in range(k, B - k):
        if hh[i] == hh[i - k:i + k + 1].max():
            piv.append((i, "H", hh[i]))
        elif ll[i] == ll[i - k:i + k + 1].min():
            piv.append((i, "L", ll[i]))
    zz = []                                   # 고점·저점이 번갈아 나오도록 정리
    for p in piv:
        if zz and zz[-1][1] == p[1]:
            if (p[1] == "H" and p[2] >= zz[-1][2]) or (p[1] == "L" and p[2] <= zz[-1][2]):
                zz[-1] = p
        else:
            zz.append(p)
    highs = [p for p in zz if p[1] == "H"]
    if not highs:
        return out
    top = max(highs, key=lambda p: p[2])     # 베이스의 왼쪽 고점 = 구간 최고점
    hs = [p for p in highs if p[0] >= top[0]]
    dep = []
    for j, p in enumerate(hs):
        end = hs[j + 1][0] if j + 1 < len(hs) else B
        seg = ll[p[0]:end]
        if len(seg):
            dep.append((p[2] - seg.min()) / p[2])
    pivot = hs[-1][2]
    out["dep"] = [round(float(d) * 100, 1) for d in dep]
    out["pivot"] = round(float(pivot))
    out["pvd"] = round(float((last / pivot - 1) * 100), 1)
    shrinking = all(dep[i] < dep[i - 1] for i in range(1, len(dep)))
    out["vcp"] = int(len(dep) >= 2 and shrinking and dep[0] <= 0.40 and dep[-1] <= 0.12
                     and B - top[0] >= 15)
    if len(dep) < 2:
        out["st"] = ""                        # 수축이 1번뿐이면 베이스로 보지 않음
    elif last > pivot and v50 > 0 and v[-1] >= 1.4 * v50 and out["pvd"] <= 5:
        out["st"] = "brk"                     # 오늘 거래량 실린 피벗 돌파
    elif -6 <= out["pvd"] <= 0:
        out["st"] = "near"                    # 피벗 아래 6% 이내
    elif out["pvd"] > 0:
        out["st"] = "above"
    else:
        out["st"] = "base"
    return out


def clean(o):
    if isinstance(o, float):
        return None if (o != o or o in (float("inf"), float("-inf"))) else o
    if isinstance(o, list):
        return [clean(x) for x in o]
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    return o


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
        # 주가 52주 신고가: 오늘 종가가 최근 52주(250거래일) 종가 중 최고 (상장 60거래일 미만 제외)
        price_nh = bool(len(dates) >= 60 and last_c >= float(sub["c"].loc[last250].max()) * 0.9999)
        from_hi = round((last_c / hi250 - 1) * 100, 1) if hi250 > 0 else None

        def rs_at(k):
            v = rs_ser.iloc[-1 - k] if len(rs_ser) > k else np.nan
            return None if pd.isna(v) else int(v)

        prev_c = float(sub["c"].iloc[-2]) if len(sub) > 1 else last_c
        try:
            sc = screen(sub, rs_at(0))
        except Exception as e:
            print("  스크리너 계산 실패:", c, e)
            sc = dict(tt=0, ttm=0, vcp=0, dep=[], pivot=None, pvd=None, vdry=None, st="")
        summary.append([
            c, u["name"], u["market"], u["sector"], u["mcap"], last_c,
            round((last_c / prev_c - 1) * 100, 2) if prev_c > 0 else 0.0,
            rs_at(0), rs_at(5), rs_at(21), rs_at(63),
            int(price_nh), 0, from_hi,   # nh = 주가 52주 신고가 (RS 라인 신호는 표시 안 함)
            dates[-1] != as_of,     # 거래정지 등으로 최근일 데이터 없음
            sc["tt"], sc["ttm"], sc["vcp"], sc["dep"], sc["pivot"], sc["pvd"], sc["vdry"], sc["st"],
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
            json.dump(clean(detail), f, ensure_ascii=False, separators=(",", ":"))

    meta = {
        "as_of": as_of,
        "built_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "count": len(summary),
        "cols": ["code", "name", "market", "sector", "mcap", "close", "chg", "rs",
                 "rs_1w", "rs_1m", "rs_3m", "price_nh", "unused", "from_hi", "stale",
                 "tt", "tt_mask", "vcp", "vcp_depths", "pivot", "pivot_dist", "vol_dry", "status"],
        "tt_names": TT_NAMES,
    }
    # 지수도 저장 (차트 비교용)
    for m in ("KOSPI", "KOSDAQ"):
        d = idx[m].iloc[-HIST_DAYS:]
        with open(os.path.join(OUT_DIR, f"idx_{m}.json"), "w") as f:
            json.dump({"d": [int(x) for x in d.index], "c": d["c"].round(2).tolist()}, f,
                      separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(clean({"meta": meta, "rows": summary}), f, ensure_ascii=False, separators=(",", ":"))
    print(f"완료: {len(summary)}개 종목, 기준일 {as_of}, {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
