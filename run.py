import argparse
import importlib.util
from pathlib import Path

# 프레임워크 엔진 및 설정 import
from framework.dataloader import DataLoader
from framework.strategy import (
    SignalRatioStrategy,
    LeveragedSignalStrategy,
    EqualWeightActiveStrategy,
    KellyMultiAssetStrategy,
)
from framework.backtester import Backtester
import config


def run():
    # 1. 어떤 알파 파일을 실행할지 명령어로 입력받음
    parser = argparse.ArgumentParser(description="모듈형 백테스팅 프레임워크")
    parser.add_argument(
        "--alpha-file",
        type=str,
        required=True,
        help="실행할 알파 레시피 파일 경로 (예: alphas/momentum_alpha.py)",
    )
    args = parser.parse_args()

    alpha_file_path = Path(args.alpha_file)
    if not alpha_file_path.exists():
        print(f"오류: 알파 파일을 찾을 수 없습니다: {alpha_file_path}")
        return

    # 2. 지정된 알파 파일을 동적으로 로드하여 'final_alpha' 객체를 가져옴
    print(f"알파 로딩 중: {alpha_file_path}")
    spec = importlib.util.spec_from_file_location(alpha_file_path.stem, alpha_file_path)
    alpha_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alpha_module)
    final_alpha = alpha_module.final_alpha

    # 3. config.py 파일의 설정을 사용하여 프레임워크 구성 요소 초기화
    print("백테스터 설정 중...")
    loader = DataLoader(
        file_path=config.DATA_FILE_PATH,
        start_date=config.START_DATE,
        end_date=config.END_DATE,
        time_col=config.TIME_COL,
        symbol_col=config.SYMBOL_COL,
        chunk_row_size=None,
        select_symbols=[
            "BTCUSDT",
            "ETHUSDT",
            "XRPUSDT",
            "SOLUSDT",
            "BNBUSDT",
            "DOGEUSDT",
            "TRXUSDT",
            "ADAUSDT",
            "HYPEUSDT",
            "LINKUSDT",
            "SUIUSDT",
            "AVAXUSDT",
            "SHIBUSDT",
            "DOTUSDT",
            "UNIUSDT",
            "AAVEUSDT",
            "1000PEPEUSDT",
        ],
    )

    strategy = SignalRatioStrategy()

    # 4. 백테스터에 모든 구성 요소를 전달하여 실행 준비
    bt = Backtester(
        data_loader=loader,
        alpha_recipe=final_alpha,
        strategy=strategy,
        capital=config.INITIAL_CAPITAL,
        collect_viz=True,
    )

    # 5. 백테스팅 실행 및 결과 출력
    print("\n" + "=" * 50)
    print("백테스팅 시작...")
    print("=" * 50)
    results = bt.run(show_dashboard=True)

    print("\n" + "=" * 50)
    print("백테스팅 완료. 결과 요약:")
    print("=" * 50)
    print(results.summary())
    # results.plot() # 나중에 시각화 기능 추가


if __name__ == "__main__":
    run()
