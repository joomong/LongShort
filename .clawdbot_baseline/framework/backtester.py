# STAR/framework/backtester.py
import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

class BacktestResult:
    def __init__(self, ret_series: pd.Series, avg_turnover: float, initial_capital: float):
        self.ret_series = pd.Series(ret_series).sort_index()
        self.avg_turnover = float(avg_turnover)
        self.initial_capital = float(initial_capital)

    def summary(self) -> str:
        if self.ret_series.empty:
            return ("--- 백테스트 결과 요약 ---\n결과 시계열이 비어 있습니다.\n--------------------------")
        BARS_PER_YEAR = 365
        equity_curve = self.initial_capital * (1.0 + self.ret_series.fillna(0.0)).cumprod()
        total_return = equity_curve.iloc[-1] / equity_curve.iloc[0] - 1.0
        vol = self.ret_series.std()
        sharpe_ratio = (self.ret_series.mean() / (vol + 1e-12)) * np.sqrt(BARS_PER_YEAR) if vol > 0 else 0.0
        dd = equity_curve / (equity_curve.cummax() + 1e-12) - 1.0
        mdd = dd.min()
        return (
            f"--- 백테스트 결과 요약 ---\n"
            f"총 수익률: {total_return:,.2%}\n"
            f"연율화 샤프 지수: {sharpe_ratio:.2f}\n"
            f"최대 낙폭 (MDD): {mdd:,.2%}\n"
            f"평균 턴오버(편도/일): {self.avg_turnover:.2%}\n"
            f"--------------------------"
        )

class Backtester:
    """
    - r_t = w_{t-1} · return_t (룩어헤드 방지)
    - 전략은 StrategyBase.generate_weights(raw_signals) 사용
    - gross=1 정규화는 전략에서 처리(너의 기본 가정에 맞춤)
    - turnover_t = 0.5 * Σ|Δw|
    """
    def __init__(self, data_loader, alpha_recipe, strategy, capital: float, *, collect_viz: bool=False):
        self.loader = data_loader
        self.alpha = alpha_recipe
        self.strategy = strategy
        self.initial_capital = float(capital)
        self.collect_viz = bool(collect_viz)

    def _set_date_index(self, df: pd.DataFrame) -> pd.DataFrame:
        """Set date/time column as index, handling various column names robustly."""
        if df.empty:
            logger.debug("sym_df is empty, skipping date index")
            return df

        # Priority order for time column detection
        time_cols = ['date', 'timestamp', 'datetime', 'time']
        found_col = None
        for col in time_cols:
            if col in df.columns:
                found_col = col
                break

        if found_col:
            logger.debug("Using '%s' column as date index", found_col)
            df = df.set_index(found_col)
            if found_col != 'date':
                df.index.name = 'date'
            return df

        # Check if index is already datetime-like
        if isinstance(df.index, pd.DatetimeIndex):
            logger.debug("Index is already DatetimeIndex, renaming to 'date'")
            df.index.name = 'date'
            return df

        # Try to use existing index if it looks like timestamps
        if df.index.dtype == 'datetime64[ns]' or df.index.dtype == 'datetime64[ns, UTC]':
            logger.debug("Index dtype is datetime, using as date index")
            df.index.name = 'date'
            return df

        # Log available columns for debugging
        logger.error(
            "No time column found. Expected one of %s, got columns: %s",
            time_cols, list(df.columns)
        )
        raise KeyError(
            f"No time column found in visualization DataFrame. "
            f"Expected one of {time_cols}, available columns: {list(df.columns)}"
        )

    def run(self, *, show_dashboard: bool=False):
        ret_history = {}
        turnover_history = {}
        minbet_history = {}
        viz_rows = []  # 시각화용(심볼 레벨) 프레임 누적

        previous_weights = pd.Series(dtype=float)
        previous_close = None

        for timestamp, data_slice in self.loader:
            # data_slice: index=instrument, columns: ['open','high','low','close', ...]
            idx = data_slice.index
            previous_weights = previous_weights.reindex(idx, fill_value=0.0)

            # 1) 수익률(자산) 및 포트 r_t
            if previous_close is not None:
                prev_close_aligned = previous_close.reindex(idx)
                asset_ret = (data_slice['close'] / prev_close_aligned) - 1.0
                asset_ret = asset_ret.fillna(0.0)
                r_t = float((previous_weights * asset_ret).sum())
                ret_history[timestamp] = r_t
            else:
                asset_ret = pd.Series(0.0, index=idx)  # 첫 바

            # 2) raw signal -> target weights (전략 사용)
            # raw_signals = self.alpha.compute(timestamp, data_slice)         # pd.Series-like
            # raw_signals = pd.Series(raw_signals, index=idx).fillna(0.0)
            # target_weights = self.strategy.generate_weights(raw_signals)    # pd.Series
            # target_weights = pd.Series(target_weights, index=idx).fillna(0.0)

            raw_signals = self.alpha.compute(timestamp, data_slice)
            raw_signals = pd.Series(raw_signals, index=idx).fillna(0.0)

            try:
                target_weights = self.strategy.generate_weights(
                    raw_signals,
                    timestamp=timestamp,
                    data_slice=data_slice,
                    previous_close=previous_close,
                )
            except TypeError:
                # 기존 전략 호환: 추가 인자를 받지 않으면 원래 시그니처로 호출
                target_weights = self.strategy.generate_weights(raw_signals)

            target_weights = pd.Series(target_weights, index=idx).fillna(0.0)

            # 3) 턴오버/미니멈 베팅
            turnover = float((target_weights - previous_weights).abs().sum() * 0.5)
            turnover_history[timestamp] = turnover
            minbet_history[timestamp] = float(target_weights.abs().mean())

            # 4) 시각화용 로우 저장(옵션)
            if self.collect_viz:
                df_row = data_slice[['open','high','low','close']].copy()
                df_row['signal']  = raw_signals
                df_row['weight']  = target_weights
                df_row['ret']     = asset_ret
                df_row['symbol']  = df_row.index
                df_row['date']    = pd.Timestamp(timestamp)
                viz_rows.append(df_row.reset_index(drop=True))

            # 5) 상태 업데이트
            previous_weights = target_weights
            previous_close = data_slice['close']

        # 시계열 정리
        ret_series = pd.Series(ret_history).fillna(0.0).sort_index()
        to_series  = pd.Series(turnover_history).fillna(0.0).sort_index()
        mb_series  = pd.Series(minbet_history).fillna(0.0).sort_index()
        avg_turnover = float(to_series.mean()) if len(to_series) else 0.0

        result = BacktestResult(ret_series=ret_series,
                                avg_turnover=avg_turnover,
                                initial_capital=self.initial_capital)

        viz_bundle = None
        if self.collect_viz:
            sym_df = pd.concat(viz_rows, ignore_index=True) if viz_rows else pd.DataFrame()
            sym_df = self._set_date_index(sym_df)
            # 포트폴리오 DF
            r = ret_series.fillna(0.0)
            equity = (1.0 + r).cumprod() * self.initial_capital
            port_df = pd.DataFrame({
                'equity': equity,
                'mdd': (equity / equity.cummax() - 1.0),
                'turnover': to_series.reindex(r.index).fillna(0.0),
                'min_bet': mb_series.reindex(r.index).fillna(0.0)
            })
            viz_bundle = {'symbols': sym_df, 'portfolio': port_df}

            if show_dashboard:
                # 런 종료 후 즉시 대시보드 오픈
                from .dashboard import launch_bokeh_dashboard
                launch_bokeh_dashboard(sym_df, port_df, title="STAR Backtest Dashboard")

        return (result, viz_bundle) if self.collect_viz else result
