# app.py
# 실행:  bokeh serve --show app.py
import numpy as np
import pandas as pd
from bokeh.io import curdoc
from bokeh.layouts import column, row
from bokeh.models import (
    ColumnDataSource, Select, HoverTool, DatetimeTickFormatter, Band, Tabs, Panel
)
from bokeh.plotting import figure

# =========================================================
# 0) 데이터 적재/연결 (여기를 네 데이터로 교체하면 됨)
# ---------------------------------------------------------
def load_data() -> pd.DataFrame:
    """
    반환 형식 (필수 컬럼):
      index: DatetimeIndex
      columns (MultiIndex 또는 일반 컬럼):
        - 'symbol' (범주)
        - 'open','high','low','close' (float)
        - 'signal' (전략 시그널, float)  # 예: {-1,0,1} 또는 z-score
        - 'ret' (단순 일수익률, float)   # 포트폴리오 계산용
        - 'weight' (해당 심볼 보유 비중, float)  # 선택사항
        - 'turnover' (해당 심볼 turnover, float) # 선택사항
    예시로 더미 데이터를 생성한다.
    """
    rng = pd.date_range("2024-01-01", periods=200, freq="D")
    syms = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    records = []
    rng_np = np.arange(len(rng))
    for s in syms:
        base = 100 + 10*np.sin(rng_np/8) + (np.random.randn(len(rng))*0.8).cumsum()
        o = base + np.random.randn(len(rng))*0.2
        c = base + np.random.randn(len(rng))*0.2
        h = np.maximum(o, c) + np.random.rand(len(rng))*0.6
        l = np.minimum(o, c) - np.random.rand(len(rng))*0.6
        sig = (np.tanh((c - o)) + np.random.randn(len(rng))*0.05)  # 예시 시그널
        ret = (pd.Series(c).pct_change().fillna(0.0)).values
        w   = np.clip(sig, -1, 1)  # 예시 weight
        to  = np.abs(pd.Series(w).diff()).fillna(0.0).values
        df_sym = pd.DataFrame({
            "symbol": s, "date": rng, "open": o, "high": h, "low": l, "close": c,
            "signal": sig, "ret": ret, "weight": w, "turnover": to
        })
        records.append(df_sym)
    df = pd.concat(records, ignore_index=True).set_index("date")
    return df

DF = load_data()
ALL_SYMBOLS = sorted(DF["symbol"].unique())
DEFAULT_SYMBOL = ALL_SYMBOLS[0]

# 포트폴리오 레벨(전 심볼 합성) 지표를 그리고 싶다면, 같은 날짜 인덱스로 집계
def build_portfolio_df(df: pd.DataFrame) -> pd.DataFrame:
    # 단순 예: 동일가중 포트폴리오 (각 심볼 ret 평균), turnover 평균
    g = df.groupby(df.index)
    port_ret = g["ret"].mean()
    port_to  = g["turnover"].mean()
    # 에쿼티/최대낙폭
    equity = (1.0 + port_ret.fillna(0.0)).cumprod()
    running_max = equity.cummax()
    mdd = (equity / running_max - 1.0).fillna(0.0)
    # min_bet: |weight|의 평균(혹은 합/최소 등, 네 기준으로 바꿔도 됨)
    # 여기선 각 시점에서 심볼별 |weight| 평균을 채택
    min_bet = g["weight"].apply(lambda s: np.nanmean(np.abs(s)) if len(s) else 0.0).fillna(0.0)
    out = pd.DataFrame({
        "equity": equity,
        "mdd": mdd,
        "turnover": port_to,
        "min_bet": min_bet
    })
    return out

PORT = build_portfolio_df(DF)

# =========================================================
# 1) 심볼별 데이터 소스 구성
# ---------------------------------------------------------
def make_symbol_df(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    s = df[df["symbol"] == symbol].copy()
    s = s.sort_index()
    # 캔들 폭(ms) - 하루 캔들 가정
    s["date"] = s.index
    s["mid"]  = (s["open"] + s["close"]) / 2
    s["dir"]  = np.where(s["close"] >= s["open"], 1, -1)  # 양봉/음봉
    # per-symbol equity/mdd (원하면)
    s["equity"] = (1.0 + s["ret"].fillna(0.0)).cumprod()
    s["equity_max"] = s["equity"].cummax()
    s["mdd"] = s["equity"] / s["equity_max"] - 1.0
    return s

def cds_from_symbol(df: pd.DataFrame, symbol: str) -> ColumnDataSource:
    s = make_symbol_df(df, symbol)
    return ColumnDataSource(s)

cds_symbol = cds_from_symbol(DF, DEFAULT_SYMBOL)

# 포트폴리오 소스
cds_port = ColumnDataSource(PORT)

# =========================================================
# 2) Figure들 만들기
# ---------------------------------------------------------
DTF = DatetimeTickFormatter(days="%Y-%m-%d")

# (A) 캔들차트
p_candle = figure(height=320, x_axis_type="datetime", title=f"Candlestick — {DEFAULT_SYMBOL}", toolbar_location="above")
p_candle.xaxis.formatter = DTF

W_MS = 24*60*60*1000 * 0.6  # 캔들 너비 (하루 * 0.6)
# high-low 꼬리
p_candle.segment(x0="date", y0="high", x1="date", y1="low", source=cds_symbol, line_width=1)
# 본체(양봉/음봉 vbar 두 개)
p_candle.vbar(x="date", width=W_MS, top="open", bottom="close", source=cds_symbol,
              fill_color="#26a69a", line_color="#26a69a", name="green",
              view=None)  # 양봉은 나중에 뷰로 걸러도 되지만 여기선 단색 처리
p_candle.vbar(x="date", width=W_MS, top="close", bottom="open", source=cds_symbol,
              fill_color="#ef5350", line_color="#ef5350", name="red")

p_candle.add_tools(HoverTool(
    tooltips=[
        ("date", "@date{%F}"),
        ("open", "@open{0.000}"), ("high", "@high{0.000}"),
        ("low", "@low{0.000}"),   ("close", "@close{0.000}"),
    ],
    formatters={"@date": "datetime"},
    mode="vline"
))

# (B) 시그널 라인
p_sig = figure(height=180, x_axis_type="datetime", title="Signal", x_range=p_candle.x_range, toolbar_location=None)
p_sig.xaxis.formatter = DTF
p_sig.line(x="date", y="signal", source=cds_symbol)
p_sig.add_tools(HoverTool(tooltips=[("date", "@date{%F}"), ("signal", "@signal{0.000}")], formatters={"@date":"datetime"}))

# (C) 포트폴리오: 누적 PnL(=equity), MDD, min_bet, turnover 탭
# Equity
p_eq = figure(height=240, x_axis_type="datetime", title="Portfolio Equity", toolbar_location=None)
p_eq.xaxis.formatter = DTF
p_eq.line(x="index", y="equity", source=cds_port)
# MDD (밴드로 음수 영역 표시)
band = Band(base="index", lower="mdd", upper=0, source=cds_port, level="underlay", fill_alpha=0.3)
p_eq.add_layout(band)
p_eq.add_tools(HoverTool(tooltips=[("date","@index{%F}"), ("equity","@equity{0.000}"), ("mdd","@mdd{0.00%}")],
                         formatters={"@index":"datetime"}))

# MDD 전용
p_mdd = figure(height=240, x_axis_type="datetime", title="Drawdown (MDD)", toolbar_location=None)
p_mdd.xaxis.formatter = DTF
p_mdd.line(x="index", y="mdd", source=cds_port)

# min bet
p_minbet = figure(height=240, x_axis_type="datetime", title="Min Bet (|weight| avg)", toolbar_location=None)
p_minbet.xaxis.formatter = DTF
p_minbet.line(x="index", y="min_bet", source=cds_port)

# turnover
p_to = figure(height=240, x_axis_type="datetime", title="Turnover", toolbar_location=None)
p_to.xaxis.formatter = DTF
p_to.line(x="index", y="turnover", source=cds_port)

tab_equity = Panel(child=column(p_eq), title="Equity")
tab_mdd    = Panel(child=column(p_mdd), title="MDD")
tab_minbet = Panel(child=column(p_minbet), title="Min Bet")
tab_to     = Panel(child=column(p_to), title="Turnover")
tabs_port  = Tabs(tabs=[tab_equity, tab_mdd, tab_minbet, tab_to])

# =========================================================
# 3) 심볼 선택 위젯 & 콜백
# ---------------------------------------------------------
sel = Select(title="Symbol", value=DEFAULT_SYMBOL, options=ALL_SYMBOLS)

def on_symbol_change(attr, old, new):
    s = make_symbol_df(DF, new)
    cds_symbol.data = ColumnDataSource.from_df(s)
    p_candle.title.text = f"Candlestick — {new}"

sel.on_change("value", on_symbol_change)

# =========================================================
# 4) 레이아웃 조립
# ---------------------------------------------------------
layout = column(
    row(sel),
    p_candle,
    p_sig,
    tabs_port,
    sizing_mode="stretch_width"
)

curdoc().add_root(layout)
curdoc().title = "Alpha Dashboard"
