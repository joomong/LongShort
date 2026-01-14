# STAR/framework/dashboard.py
# Bokeh 3.8.0 전용
import numpy as np
import pandas as pd

from bokeh.layouts import column, row
from bokeh.models import (
    ColumnDataSource, Select, HoverTool, DatetimeTickFormatter,
    Band, Tabs, TabPanel, NumeralTickFormatter
)
from bokeh.plotting import figure
from bokeh.server.server import Server


# --- 단독 심볼 전략 계산 파라미터(간단 비용 모델) ---
# 필요하면 여기 fee_rate만 바꿔 써도 됨.
_SINGLE_FEE_RATE = 0.0  # 편도 수수료 (0.0005 등)


def _compute_single_symbol_strategy_frame(s: pd.DataFrame, fee_rate: float) -> pd.DataFrame:
    """
    입력 s: 단일 심볼의 시계열 DataFrame (date, open, high, low, close, signal, ret ...)
    반환: s에 아래 컬럼을 추가해서 리턴
      - pos: sign(signal) → {-1,0,1}
      - prev_pos: pos.shift(1)
      - single_turnover: 0.5*|Δpos|
      - single_ret_gross: prev_pos * ret
      - single_cost: 2*fee_rate*single_turnover
      - single_ret_net: single_ret_gross - single_cost
      - single_cum_equity: (1+single_ret_net).cumprod()
    """
    s = s.copy()
    if "signal" not in s:
        s["signal"] = 0.0
    if "ret" not in s:
        s["ret"] = 0.0

    # 포지션: 신호 부호만 사용 (롱/숏/캐시)
    s["pos"] = np.sign(s["signal"]).clip(-1, 1).astype(float)
    s["prev_pos"] = s["pos"].shift(1).fillna(0.0)

    # 단일 심볼 턴오버 / 비용
    s["single_turnover"] = 0.5 * (s["pos"] - s["prev_pos"]).abs()
    s["single_ret_gross"] = s["prev_pos"] * s["ret"]

    if fee_rate and fee_rate > 0:
        s["single_cost"] = 2.0 * float(fee_rate) * s["single_turnover"]
    else:
        s["single_cost"] = 0.0

    s["single_ret_net"] = s["single_ret_gross"] - s["single_cost"]
    s["single_cum_equity"] = (1.0 + s["single_ret_net"].fillna(0.0)).cumprod()

    return s


def _make_symbol_source(sym_df: pd.DataFrame, symbol: str, fee_rate: float) -> ColumnDataSource:
    """
    sym_df: index=DatetimeIndex, columns ⊇
        ['symbol','open','high','low','close','signal','weight','ret', ...]
    반환: 선택 심볼의 캔들/시그널 + 단독 전략 결과가 포함된 ColumnDataSource
    """
    empty_cols = [
        "date", "open", "high", "low", "close",
        "signal", "ret",
        "body_top", "body_bottom", "color",
        # single-symbol strategy outputs:
        "pos", "prev_pos", "single_turnover",
        "single_ret_gross", "single_cost", "single_ret_net", "single_cum_equity",
    ]

    if sym_df is None or len(sym_df) == 0:
        return ColumnDataSource(pd.DataFrame(columns=empty_cols))

    s = sym_df[sym_df["symbol"] == symbol].copy() if symbol else sym_df.iloc[0:0].copy()
    s = s.sort_index()

    if s.empty:
        return ColumnDataSource(pd.DataFrame(columns=empty_cols))

    # 인덱스 → date 컬럼으로 승격
    s.index = pd.to_datetime(s.index)
    s.index.name = "date"
    s.reset_index(inplace=True)

    # 결측 대비
    for col in ("open", "high", "low", "close"):
        if col not in s:
            s[col] = np.nan
    if "signal" not in s:
        s["signal"] = 0.0
    if "ret" not in s:
        s["ret"] = 0.0

    # 단독 전략 계산
    s = _compute_single_symbol_strategy_frame(s, fee_rate=fee_rate)

    # 캔들 본체와 색상(양봉=초록, 음봉=빨강)
    s["body_top"] = s[["open", "close"]].max(axis=1)
    s["body_bottom"] = s[["open", "close"]].min(axis=1)
    s["color"] = np.where(s["close"] >= s["open"], "#26a69a", "#ef5350")

    return ColumnDataSource(s)


def _build_app(doc, sym_df: pd.DataFrame, port_df: pd.DataFrame, title: str):
    """
    sym_df: index=datetime, cols ⊇
        ['symbol','open','high','low','close','signal','ret', ...]
    port_df: index=datetime, cols ⊇ ['equity','mdd','turnover','min_bet']
    """
    # 심볼 초기화
    all_symbols = sorted(sym_df["symbol"].dropna().unique().tolist()) if sym_df is not None and len(sym_df) else []
    default_symbol = all_symbols[0] if all_symbols else ""

    # 데이터 소스 (심볼 단독 전략 계산 포함)
    cds_symbol = _make_symbol_source(sym_df, default_symbol, fee_rate=_SINGLE_FEE_RATE)

    # 포트 소스
    port = (port_df.copy() if port_df is not None else
            pd.DataFrame(index=pd.to_datetime([]), columns=["equity", "mdd", "turnover", "min_bet"]))
    port.index = pd.to_datetime(port.index)
    port.index.name = "date"
    cds_port = ColumnDataSource(port.reset_index())

    # 포맷/툴팁
    DTF = DatetimeTickFormatter(days="%Y-%m-%d")

    hover_candle = HoverTool(
        tooltips=[("date", "@date{%F}"), ("open", "@open{0.000}"),
                  ("high", "@high{0.000}"), ("low", "@low{0.000}"),
                  ("close", "@close{0.000}")],
        formatters={"@date": "datetime"},
        mode="vline",
    )
    hover_signal = HoverTool(
        tooltips=[("date", "@date{%F}"),
                  ("signal", "@signal{0.000}")],
        formatters={"@date": "datetime"},
    )
    hover_cumeq = HoverTool(
        tooltips=[("date", "@date{%F}"),
                  ("cum equity (single)", "@single_cum_equity{0.000}")],
        formatters={"@date": "datetime"},
    )
    hover_port = HoverTool(
        tooltips=[("date", "@date{%F}"),
                  ("equity", "@equity{0.000}"),
                  ("mdd", "@mdd{0.00%}")],
        formatters={"@date": "datetime"},
    )

    # (A) 캔들차트 + 시그널
    p_candle = figure(height=320, x_axis_type="datetime",
                      title=f"Candlestick — {default_symbol or '-'}",
                      toolbar_location="above")
    p_candle.xaxis.formatter = DTF
    W = 24 * 60 * 60 * 1000 * 0.6  # 1D * 0.6
    p_candle.segment(x0="date", y0="high", x1="date", y1="low", source=cds_symbol, line_width=1)
    p_candle.vbar(x="date", width=W, top="body_top", bottom="body_bottom",
                  source=cds_symbol, fill_color="color", line_color="color")
    p_candle.add_tools(hover_candle)

    p_sig = figure(height=160, x_axis_type="datetime", title="Signal",
                   x_range=p_candle.x_range, toolbar_location=None)
    p_sig.xaxis.formatter = DTF
    p_sig.line(x="date", y="signal", source=cds_symbol)
    p_sig.add_tools(hover_signal)

    # (B) 단독(Per-Symbol) 전략: 누적 에쿼티 (큰 사이즈, 단독 탭)
    p_cumeq_big = figure(
        height=320, x_axis_type="datetime",
        title="Per-Symbol Strategy — Cumulative Equity (base=1.0)",
        x_range=p_candle.x_range, toolbar_location=None,
    )
    p_cumeq_big.xaxis.formatter = DTF
    p_cumeq_big.line(x="date", y="single_cum_equity", source=cds_symbol, line_width=2)
    p_cumeq_big.add_tools(hover_cumeq)

    tabs_symbol = Tabs(tabs=[
        TabPanel(child=column(p_cumeq_big), title="Per-Symbol Equity"),
    ])

    # (C) 포트폴리오 탭 (기존)
    p_eq = figure(height=240, x_axis_type="datetime", title="Portfolio Equity", toolbar_location=None)
    p_eq.xaxis.formatter = DTF
    p_eq.line(x="date", y="equity", source=cds_port)
    band = Band(base="date", lower="mdd", upper=0, source=cds_port, level="underlay", fill_alpha=0.30)
    p_eq.add_layout(band)
    p_eq.add_tools(hover_port)

    p_mdd = figure(height=220, x_axis_type="datetime", title="Drawdown (MDD)", toolbar_location=None)
    p_mdd.xaxis.formatter = DTF
    p_mdd.line(x="date", y="mdd", source=cds_port)

    p_minbet = figure(height=220, x_axis_type="datetime", title="Min Bet (|weight| avg)", toolbar_location=None)
    p_minbet.xaxis.formatter = DTF
    p_minbet.line(x="date", y="min_bet", source=cds_port)

    p_to = figure(height=220, x_axis_type="datetime", title="Turnover", toolbar_location=None)
    p_to.xaxis.formatter = DTF
    p_to.line(x="date", y="turnover", source=cds_port)

    tabs_port = Tabs(tabs=[
        TabPanel(child=column(p_eq), title="Equity"),
        TabPanel(child=column(p_mdd), title="MDD"),
        TabPanel(child=column(p_minbet), title="Min Bet"),
        TabPanel(child=column(p_to), title="Turnover"),
    ])

    # 심볼 선택
    sel = Select(title="Symbol", value=default_symbol, options=all_symbols)

    def on_symbol_change(attr, old, new):
        new_src = _make_symbol_source(sym_df, new, fee_rate=_SINGLE_FEE_RATE)
        cds_symbol.data = dict(new_src.data)  # shallow copy (Bokeh 3.8 안전)
        p_candle.title.text = f"Candlestick — {new or '-'}"

    sel.on_change("value", on_symbol_change)

    # 레이아웃
    layout = column(
        row(sel),
        p_candle,
        p_sig,
        tabs_symbol,   # (단독 심볼 누적 에쿼티 - 크게)
        tabs_port,     # (기존 포트 탭)
        sizing_mode="stretch_width",
    )
    doc.add_root(layout)
    doc.title = title


def launch_bokeh_dashboard(sym_df: pd.DataFrame, port_df: pd.DataFrame,
                           title: str = "STAR Backtest Dashboard", port: int = 0):
    """
    백테스트 종료 시 호출하면 브라우저에 대시보드가 뜬다.
    - sym_df: index=datetime, cols ['symbol','open','high','low','close','signal','ret',...]
    - port_df: index=datetime, cols ['equity','mdd','turnover','min_bet']
    """
    def bkapp(doc):
        _build_app(doc, sym_df, port_df, title)

    server = Server({"/": bkapp}, port=port)
    server.start()
    server.io_loop.add_callback(server.show, "/")
    try:
        server.io_loop.start()
    except KeyboardInterrupt:
        pass

