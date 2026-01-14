# --- 데이터 설정 ---
DATA_FILE_PATH = "Data/um_1d_all.parquet"

# Parquet 파일 내의 실제 컬럼 이름
TIME_COL = "open_time"
SYMBOL_COL = "symbol"

# 슬라이스에 포함할 컬럼들 (없어도 동작은 하지만 캔들/시그널용 권장)
COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume", "turnover", "value"]

# --- 백테스팅 기간 설정 ---
START_DATE = "2020-01-01"
END_DATE   = "2025-08-31"   # inclusive

# --- 포트폴리오 설정 ---
INITIAL_CAPITAL = 1000000

# --- 심볼 선택 (여기만 바꿔서 필터링) ---
# 1) 전체: None
# 2) 정확 매칭 리스트: ["BTCUSDT", "ETHUSDT"]
# 3) 와일드카드: ["*USDT"] 또는 "BTC*,ETHUSDT" 처럼 문자열로도 OK
SYMBOLS = ["*USDT"]   # None 로 두면 전체 심볼
# SYMBOLS = None
# SYMBOLS = "BTCUSDT,ETHUSDT"
