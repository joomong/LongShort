# STAR/framework/dataloader.py
import pandas as pd
import pyarrow.dataset as ds
from pathlib import Path
from typing import Optional, Tuple, Iterator, List

class DataLoader:
    """
    PyArrow Dataset을 사용하여 Parquet 파일을 효율적으로 필터링하고 로드하는 클래스.
    - 모든 시간은 UTC 기준으로 처리 (tz-aware pd.Timestamp).
    - 기간 필터는 UTC로 고정하고, 끝일은 inclusive (END_DATE 23:59:59.999)로 포함.
    - 청킹 모드에서도 '타임스탬프 단위'로 완전한 슬라이스를 내보냄
      (마지막 타임스탬프는 다음 청크와 합쳐서 내보내는 carry-over 처리).

    Parameters
    ----------
    file_path : str
        Parquet 파일 경로 (단일 파일 또는 파티션 디렉토리 경로 모두 가능)
    start_date, end_date : str
        'YYYY-MM-DD' 형태 권장. UTC 기준으로 해석.
    time_col : str
        밀리초(ms) 단위 epoch 타임스탬프가 들어있는 컬럼명
    symbol_col : str
        종목/심볼 컬럼명
    chunk_row_size : Optional[int]
        None이면 전체 로딩 모드. 정수이면 해당 행 수 단위로 청킹.
    select_columns : Optional[List[str]]
        불필요한 컬럼 로딩을 줄이고 싶을 때 사용. time_col, symbol_col은 자동 포함됨.
    """

    def __init__(
        self,
        file_path: str,
        start_date: str,
        end_date: str,
        time_col: str,
        symbol_col: str,
        chunk_row_size: Optional[int] = None,
        select_columns: Optional[List[str]] = None,
        select_symbols: Optional[List[str]] = None,  # <-- 이 라인 추가
    ):
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"데이터 파일을 찾을 수 없습니다: {self.file_path}")

        self.time_col = time_col
        self.symbol_col = symbol_col
        self.chunk_row_size = chunk_row_size

        # --- UTC 경계: 정수 ns → ms 변환으로 float 오차 제거 (inclusive end) ---
        start_ts_ms = pd.Timestamp(start_date, tz="UTC").value // 1_000_000
        end_ts_ms = (
            pd.Timestamp(end_date, tz="UTC")
            + pd.Timedelta(days=1)
            - pd.Timedelta(milliseconds=1)
        ).value // 1_000_000

        # self.filter_expression = (ds.field(time_col) >= start_ts_ms) & (
        #     ds.field(time_col) <= end_ts_ms
        # )

        # 1. 필수 시간 필터를 먼저 정의합니다.
        time_filter = (ds.field(time_col) >= start_ts_ms) & (
            ds.field(time_col) <= end_ts_ms
        )

        # 2. select_symbols 인자가 제공되었는지 확인합니다.
        if select_symbols is not None and len(select_symbols) > 0:
            # 3. Symbol 필터를 생성합니다. (isin 사용)
            symbol_filter = ds.field(symbol_col).isin(select_symbols)
            # 4. 시간 필터와 Symbol 필터를 '&' 연산자로 결합합니다.
            self.filter_expression = time_filter & symbol_filter
        else:
            # select_symbols가 없으면 기존처럼 시간 필터만 사용합니다.
            self.filter_expression = time_filter

        # 최소 컬럼 집합 구성
        self._columns = None
        if select_columns is not None:
            base_cols = {self.time_col, self.symbol_col}
            self._columns = list(base_cols.union(select_columns))

        # Dataset 구성
        self.dataset = ds.dataset(self.file_path, format="parquet")

        # 내부 상태
        self._iter_index: int = 0

        # carry-over: 청크 경계에서 "마지막 타임스탬프"의 불완전 슬라이스를 다음 청크와 합치기 위해 보관
        self._carry_last_ts_df: Optional[pd.DataFrame] = None
        self._carry_last_ts: Optional[pd.Timestamp] = None

        # 이터레이션용 현재 내보낼 df/타임스탬프 목록
        self._emit_df: Optional[pd.DataFrame] = None
        self._emit_timestamps: Optional[pd.Index] = None

        # --- 로딩 모드 분기 ---
        if self.chunk_row_size is None:
            self._full_data: pd.DataFrame = self._load_all_data()
            self._all_timestamps = (
                self._full_data.index.get_level_values(0).unique().sort_values()
            )
        else:
            self.batch_iterator: Optional[Iterator] = None

    # ---------- 내부 유틸 ----------

    def _arrow_table_to_df(self, table) -> pd.DataFrame:
        """Arrow Table → 표준화된 MultiIndex DataFrame(timestamp[UTC], instrument)."""
        df = table.to_pandas()
        if df.empty:
            return pd.DataFrame()

        if self.time_col not in df.columns or self.symbol_col not in df.columns:
            raise KeyError(
                f"필수 컬럼 누락: time_col='{self.time_col}', symbol_col='{self.symbol_col}', "
                f"현재 컬럼: {list(df.columns)}"
            )

        df = df.rename(columns={self.time_col: "timestamp", self.symbol_col: "instrument"})
        # ms epoch → UTC tz-aware Timestamp
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

        # 정렬 및 MultiIndex
        df = df.sort_values(by=["timestamp", "instrument"])
        df = df.set_index(["timestamp", "instrument"])
        return df

    def _load_all_data(self) -> pd.DataFrame:
        """(전체 로딩) PyArrow 필터 적용 후 한 번에 로드."""
        print("청킹 없이 전체 파일 로딩 중... (PyArrow 필터 적용, UTC 경계)")
        scanner = self.dataset.scanner(
            filter=self.filter_expression, columns=self._columns
        )
        table = scanner.to_table()
        df = self._arrow_table_to_df(table)
        if df.empty:
            print("경고: 전체 로딩 결과, 해당 기간에 데이터가 없습니다.")
        else:
            print("전체 파일 로딩 및 전처리 완료.")
        return df

    def _prepare_emit_from_chunk_df(self, df: pd.DataFrame):
        """
        현재 청크 df를 받아,
        - 이전 청크의 carry(last ts)와 병합
        - 이번 청크에서 '마지막 타임스탬프'는 carry로 보류
        - 나머지 타임스탬프들만 이번 이터레이션에서 내보낼 준비
        """
        if df.empty and self._carry_last_ts_df is None:
            # 비어있으면 내보낼 것도, carry도 없음
            self._emit_df = None
            self._emit_timestamps = None
            return

        # 1) carry가 있으면 합치고, 그 타임스탬프의 행을 df에서 제거하여 이중 포함 방지
        if self._carry_last_ts_df is not None and self._carry_last_ts is not None:
            same_ts_rows = df.loc[df.index.get_level_values(0) == self._carry_last_ts]
            if not same_ts_rows.empty:
                # carry + 현재 청크 동일 ts 합치기
                merged = pd.concat([self._carry_last_ts_df, same_ts_rows])
                # 정렬 보장
                merged = merged.sort_index()
                # df에서 해당 ts 행 제거 (emit_df에는 carry 합친 걸 사용)
                df = df.drop(index=self._carry_last_ts, level=0)
                # 새로운 df로 교체: carry된 ts는 merged로, 나머지는 df 그대로
                df = pd.concat([merged, df]).sort_index()
            # carry는 일단 해제(아래에서 다시 설정할 수 있음)
            self._carry_last_ts_df = None
            self._carry_last_ts = None

        if df.empty:
            self._emit_df = None
            self._emit_timestamps = None
            return

        # 2) 이번 청크의 타임스탬프들
        ts_unique = df.index.get_level_values(0).unique().sort_values()

        if len(ts_unique) == 1:
            # 타임스탬프가 1개뿐이면 전부 carry로 보유(다음 청크에서 이어붙임)
            self._carry_last_ts = ts_unique[-1]
            self._carry_last_ts_df = df.loc[self._carry_last_ts]
            self._emit_df = None
            self._emit_timestamps = None
            return

        # 3) 마지막 타임스탬프는 carry로 보유, 나머지는 emit
        last_ts = ts_unique[-1]
        self._carry_last_ts = last_ts
        self._carry_last_ts_df = df.loc[last_ts]

        emit_ts = ts_unique[:-1]
        emit_df = df.loc[emit_ts]

        self._emit_df = emit_df
        self._emit_timestamps = emit_ts

    def _load_next_chunk(self):
        """(청킹 모드) 다음 청크를 로드하고 emit 준비."""
        try:
            batch = next(self.batch_iterator)
        except StopIteration:
            # 더 이상 배치가 없으면, carry만 남아있다면 그걸 마지막으로 emit
            if self._carry_last_ts_df is not None and self._carry_last_ts is not None:
                df = self._carry_last_ts_df.copy()
                df.index = pd.MultiIndex.from_product(
                    [[self._carry_last_ts], df.index], names=["timestamp", "instrument"]
                )
                # 이번 호출에서 전부 emit하고 carry 비움
                self._carry_last_ts_df = None
                self._carry_last_ts = None
                self._emit_df = df
                self._emit_timestamps = pd.Index([df.index.get_level_values(0)[0]])
                self._iter_index = 0
                return
            # 정말 끝
            self._emit_df = None
            self._emit_timestamps = None
            return

        # batch -> DataFrame 정규화
        table = batch.to_table()
        if self._columns is not None:
            # 배치에도 컬럼 선택 적용
            table = table.select(self._columns)
        df = self._arrow_table_to_df(table)

        # 이번 청크에서 emit/carry 분리
        self._prepare_emit_from_chunk_df(df)
        self._iter_index = 0

    # ---------- 이터레이터 프로토콜 ----------

    def __iter__(self) -> "DataLoader":
        self._iter_index = 0
        if self.chunk_row_size is not None:
            print("DataLoader 이터레이션 시작 (청킹 모드 / UTC 경계 / 안전한 타임스탬프 emit)")
            self.batch_iterator = self.dataset.to_batches(
                filter=self.filter_expression,
                batch_size=self.chunk_row_size,
                columns=self._columns,
            )
            # 첫 청크 로드
            self._load_next_chunk()
        else:
            print("DataLoader 이터레이션 시작 (전체 로딩 모드 / UTC 경계)")
        return self

    def __next__(self) -> Tuple[pd.Timestamp, pd.DataFrame]:
        # 전체 로딩 모드
        if self.chunk_row_size is None:
            if self._iter_index >= len(self._all_timestamps):
                raise StopIteration
            current_ts = self._all_timestamps[self._iter_index]
            data_slice = self._full_data.loc[current_ts]
            self._iter_index += 1
            return current_ts, data_slice

        # 청킹 모드
        while True:
            # emit 준비가 없으면 다음 청크 로드
            if self._emit_df is None or self._emit_timestamps is None or len(self._emit_timestamps) == 0:
                self._load_next_chunk()
                if self._emit_df is None or self._emit_timestamps is None or len(self._emit_timestamps) == 0:
                    raise StopIteration

            if self._iter_index >= len(self._emit_timestamps):
                # 현재 emit 소진 → 다음 청크
                self._load_next_chunk()
                if self._emit_df is None or self._emit_timestamps is None or len(self._emit_timestamps) == 0:
                    raise StopIteration

            current_ts = self._emit_timestamps[self._iter_index]
            data_slice = self._emit_df.loc[current_ts]
            self._iter_index += 1
            return current_ts, data_slice
