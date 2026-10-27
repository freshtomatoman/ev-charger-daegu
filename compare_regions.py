"""
대구·구미 수집 자료 비교 (지역 결정용)

사용법 (저장소 루트에서, 또는 Colab에서 저장소를 clone한 뒤)
    python compare_regions.py --root . --regions daegu gumi --since "2026-10-27 18:00"

collect.py가 만든 data/<지역>/status, data/<지역>/static, logs/<지역> 를 읽어
아래 표를 출력하고 region_compare.csv 로 저장합니다.

    급속 충전기 수 · 급속 충전소 수 · 수집 회차 · 이상 상태 비율 · 이용 가능 비율
    급속 충전기 1시간당 2↔3 전환 비율 (가장 중요) · 급속 충전소 1시간당 '빈 자리 있음' 변화 비율
    운영기관 구성(상위 3) · 기관별 상태 갱신 지연 중앙값 · 수집 성공률 · 하루 예상 호출 수
"""
import argparse
import glob
import os

import numpy as np
import pandas as pd

FAST_KW = 50
VALID = (2, 3)


def read_many(pattern):
    files = sorted(glob.glob(pattern, recursive=True))
    if not files:
        return pd.DataFrame()
    return pd.concat((pd.read_csv(f, dtype=str) for f in files), ignore_index=True)


def ts(s):
    return pd.to_datetime(s.astype(str).str.slice(0, 14), format="%Y%m%d%H%M%S", errors="coerce")


def load_region(root, region, since=None):
    st = read_many(os.path.join(root, "data", region, "status", "**", "*.csv.gz"))
    sc = read_many(os.path.join(root, "data", region, "static", "*.csv.gz"))
    if st.empty:
        raise FileNotFoundError(f"{region}: 상태 파일이 없습니다")
    st["chgerId"] = st["chgerId"].str.zfill(2)
    st["key"] = st["statId"] + "_" + st["chgerId"]
    st["t"] = pd.to_datetime(st["collected_at_kst"].str.slice(0, 19), errors="coerce")
    if since:
        st = st[st["t"] >= pd.Timestamp(since)]
    st["stat"] = pd.to_numeric(st["stat"], errors="coerce")
    st["upd"] = ts(st["statUpdDt"])
    if not sc.empty:
        sc["chgerId"] = sc["chgerId"].str.zfill(2)
        sc["key"] = sc["statId"] + "_" + sc["chgerId"]
        attrs = sc.drop_duplicates("key", keep="last").set_index("key")
        for c in ["output", "busiId", "kind", "chgerType"]:
            st[c] = st["key"].map(attrs[c]) if c in attrs else np.nan
    st["output_kw"] = pd.to_numeric(st.get("output"), errors="coerce")
    # 수집 회차: 3분 넘게 벌어지면 새 회차
    ut = np.sort(st["t"].dropna().unique())
    run = np.cumsum(np.r_[True, np.diff(ut) > np.timedelta64(3, "m")]) - 1
    st["run"] = st["t"].map(pd.Series(run, index=ut))
    return st.sort_values(["key", "t"]).reset_index(drop=True)


def per_hour_change(d, key, state):
    """연속 관측(15분 이내) 사이 상태 변화 수 / 관측 시간(시간)."""
    d = d.sort_values([key, "t"])
    gap = d.groupby(key)["t"].diff().dt.total_seconds() / 60
    prev = d.groupby(key)[state].shift()
    ok = gap <= 15
    changes = (ok & (d[state] != prev)).sum()
    hours = gap[ok].sum() / 60
    return changes / hours if hours > 0 else np.nan, int(changes), hours


def summarize(root, region, since=None):
    d = load_region(root, region, since)
    f = d[d["output_kw"] >= FAST_KW]
    fv = f[f["stat"].isin(VALID)].copy()
    out = {"지역": region,
           "충전기 수(전체)": d["key"].nunique(),
           "급속 충전기 수": f["key"].nunique(),
           "급속 충전소 수": f["statId"].nunique(),
           "수집 회차": d["run"].nunique(),
           "기간(시간)": round((d["t"].max() - d["t"].min()).total_seconds() / 3600, 1),
           "이상 상태 비율(급속)": round(1 - f["stat"].isin(VALID).mean(), 4),
           "이용 가능 비율(급속)": round((fv["stat"] == 2).mean(), 4)}
    rate, n, hrs = per_hour_change(fv, "key", "stat")
    out["급속 충전기 1시간당 2↔3 전환"] = round(rate, 4)
    out["관측된 전환 수(급속)"] = n
    # 충전소 단위: 회차마다 '빈 급속 충전기 1대 이상' 여부
    s = fv.groupby(["statId", "run"]).agg(t=("t", "min"), avail=("stat", lambda x: int((x == 2).any())))
    s = s.reset_index()
    srate, sn, _ = per_hour_change(s, "statId", "avail")
    out["급속 충전소 1시간당 빈자리 변화"] = round(srate, 4)
    out["충전소 빈자리 있음 비율"] = round(s["avail"].mean(), 4)
    # 운영기관 구성과 갱신 지연
    comp = f.drop_duplicates("key")["busiId"].value_counts(normalize=True).head(3)
    out["운영기관 상위3(비중)"] = ", ".join(f"{k} {v:.0%}" for k, v in comp.items())
    lag = (f["t"] - f["upd"]).dt.total_seconds() / 60
    lag_by = lag.groupby(f["busiId"]).median().reindex(comp.index).round(1)
    out["상위3 갱신지연 중앙값(분)"] = ", ".join(f"{k} {v}" for k, v in lag_by.items())
    # 수집 로그
    logs = read_many(os.path.join(root, "logs", region, "*.csv"))
    if not logs.empty:
        if since:
            logs = logs[pd.to_datetime(logs["run_started_kst"].str.slice(0, 19)) >= pd.Timestamp(since)]
        out["수집 성공률(로그)"] = round((logs["status"] == "ok").mean(), 4)
        out["하루 예상 호출 수"] = int(pd.to_numeric(logs["pages"], errors="coerce").median() * 144)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--regions", nargs="+", default=["daegu", "gumi"])
    ap.add_argument("--since", default=None, help='비교 시작 시각, 예: "2026-10-27 18:00"')
    ap.add_argument("--ratio", type=float, default=2.0, help="대구 전환 기준 배수")
    a = ap.parse_args()
    table = pd.DataFrame([summarize(a.root, r, a.since) for r in a.regions]).set_index("지역").T
    pd.set_option("display.width", 200)
    print(table.to_string())
    table.to_csv("region_compare.csv", encoding="utf-8-sig")
    if {"daegu", "gumi"} <= set(a.regions):
        k = "급속 충전기 1시간당 2↔3 전환"
        dg, gm = table.loc[k, "daegu"], table.loc[k, "gumi"]
        ratio = dg / gm if gm and gm > 0 else np.inf
        print(f"\n대구/구미 급속 전환 비율 = {ratio:.2f}배 (기준 {a.ratio}배)")
        print("→ 대구로 전환 권장" if ratio >= a.ratio else "→ 차이가 작음: 구미 유지 또는 더 모아서 재확인")
        if min(table.loc["관측된 전환 수(급속)"]) < 30:
            print("주의: 관측된 전환이 30건 미만이라 판단이 불안정합니다. 몇 시간 더 모아 다시 실행하세요.")


if __name__ == "__main__":
    main()
