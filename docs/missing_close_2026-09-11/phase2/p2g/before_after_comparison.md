# 정정 전후 정산 로그 비교

- 판정: **PASS**
- 변경된 정산 행 25256건, 변경된 종목 1585개
- 목록 밖이지만 exposure만 움직인 종목 919개 / 7690행 — gross 1 정규화의 파급이며 회귀 아님 (최대 exposure 차 5.29e-05, 손익 영향 +0.000054)

## 알파·유니버스별 합계

| 유니버스 | alpha | R | 정산손익 전 | 후 | 차이 | 변경행 | 추가 | 삭제 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ALL | afl_100_zscore | -1 | -0.036716 | -0.039954 | -0.003238 | 2050 | 634 | 633 |
| ALL | afl_100_zscore | 0 | -0.001668 | -0.001796 | -0.000128 | 2050 | 634 | 633 |
| ALL | afl_963_zscore | -1 | +0.008078 | +0.004960 | -0.003118 | 1251 | 507 | 507 |
| ALL | afl_963_zscore | 0 | +0.000838 | +0.000735 | -0.000102 | 1251 | 507 | 507 |
| ALL | close_tsmean20_zscore | -1 | +0.111446 | +0.114417 | +0.002971 | 2099 | 637 | 636 |
| ALL | close_tsmean20_zscore | 0 | +0.002886 | +0.007194 | +0.004309 | 2099 | 637 | 636 |
| ALL | neg_returns_tsmean5_zscore | -1 | -0.007400 | -0.005255 | +0.002145 | 2159 | 637 | 636 |
| ALL | neg_returns_tsmean5_zscore | 0 | +0.001626 | +0.000922 | -0.000704 | 2159 | 637 | 636 |
| ALL | returns_zscore | -1 | +0.074225 | +0.077487 | +0.003262 | 2139 | 637 | 636 |
| ALL | returns_zscore | 0 | -0.000003 | +0.000843 | +0.000845 | 2139 | 637 | 636 |
| Top1000 | afl_100_zscore | -1 | +0.043222 | +0.038496 | -0.004727 | 430 | 191 | 191 |
| Top1000 | afl_100_zscore | 0 | -0.000542 | -0.000380 | +0.000162 | 430 | 191 | 191 |
| Top1000 | afl_963_zscore | -1 | +0.016359 | +0.007109 | -0.009250 | 412 | 185 | 185 |
| Top1000 | afl_963_zscore | 0 | +0.002569 | +0.002401 | -0.000168 | 412 | 185 | 185 |
| Top1000 | close_tsmean20_zscore | -1 | +0.027232 | +0.026903 | -0.000329 | 482 | 192 | 192 |
| Top1000 | close_tsmean20_zscore | 0 | -0.000633 | -0.000587 | +0.000046 | 482 | 192 | 192 |
| Top1000 | neg_returns_tsmean5_zscore | -1 | +0.014390 | +0.011636 | -0.002755 | 481 | 192 | 192 |
| Top1000 | neg_returns_tsmean5_zscore | 0 | +0.002450 | +0.001982 | -0.000468 | 481 | 192 | 192 |
| Top1000 | returns_zscore | -1 | +0.009055 | +0.010222 | +0.001168 | 488 | 192 | 192 |
| Top1000 | returns_zscore | 0 | +0.001469 | +0.002874 | +0.001405 | 488 | 192 | 192 |
| Top500 | afl_100_zscore | -1 | +0.033652 | +0.010989 | -0.022664 | 128 | 57 | 57 |
| Top500 | afl_100_zscore | 0 | -0.000219 | -0.000496 | -0.000277 | 128 | 57 | 57 |
| Top500 | afl_963_zscore | -1 | +0.010131 | +0.001803 | -0.008328 | 127 | 57 | 57 |
| Top500 | afl_963_zscore | 0 | +0.000420 | +0.000498 | +0.000078 | 127 | 57 | 57 |
| Top500 | close_tsmean20_zscore | -1 | +0.000713 | +0.001435 | +0.000722 | 127 | 57 | 57 |
| Top500 | close_tsmean20_zscore | 0 | +0.000308 | +0.000247 | -0.000061 | 127 | 57 | 57 |
| Top500 | neg_returns_tsmean5_zscore | -1 | +0.008519 | +0.006690 | -0.001829 | 127 | 57 | 57 |
| Top500 | neg_returns_tsmean5_zscore | 0 | +0.000463 | -0.000253 | -0.000717 | 127 | 57 | 57 |
| Top500 | returns_zscore | -1 | +0.021799 | +0.011921 | -0.009878 | 128 | 57 | 57 |
| Top500 | returns_zscore | 0 | -0.001134 | -0.000374 | +0.000760 | 128 | 57 | 57 |

## 변경된 종목 (정산일 손익 차이 내림차순)

| 유니버스 | alpha | R | symbol | 정산일 | source 전→후 | exposure 차 | return 차 | transferred 차 | 손익 차 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ALL | returns_zscore | -1 | 687847664 | 2025-12-24 | nan → default | - | - | - | +0.012666 |
| ALL | returns_zscore | -1 | 687847664 | 2026-04-08 | default → nan | - | - | - | -0.012664 |
| ALL | close_tsmean20_zscore | 0 | 34204761 | 2014-05-01 | nan → computed | - | - | - | +0.007563 |
| ALL | close_tsmean20_zscore | -1 | 34204761 | 2014-05-01 | nan → computed | - | - | - | +0.007563 |
| ALL | neg_returns_tsmean5_zscore | -1 | 687847664 | 2025-12-24 | nan → default | - | - | - | +0.006168 |
| ALL | neg_returns_tsmean5_zscore | -1 | 687847664 | 2026-04-08 | default → nan | - | - | - | -0.006167 |
| ALL | returns_zscore | -1 | 102373675 | 2021-05-21 | nan → default | - | - | - | -0.005874 |
| ALL | returns_zscore | -1 | 102373675 | 2021-06-07 | default → nan | - | - | - | +0.005854 |
| Top500 | afl_963_zscore | -1 | 2591859 | 2026-08-17 | default → computed | 0.000000 | 0.966002 | 0.005774 | -0.005774 |
| Top500 | close_tsmean20_zscore | -1 | 2595940 | 2010-02-16 | default → computed | 0.000000 | 0.997904 | 0.000000 | +0.005632 |
| Top500 | returns_zscore | -1 | 12571130 | 2011-12-02 | default → computed | 0.000000 | 1.000000 | 0.000000 | -0.004587 |
| ALL | neg_returns_tsmean5_zscore | -1 | 2663642 | 2025-06-25 | default → nan | - | - | - | +0.004534 |
| ALL | neg_returns_tsmean5_zscore | -1 | 2663642 | 2025-05-19 | nan → default | - | - | - | -0.004534 |
| ALL | afl_100_zscore | -1 | 690615075 | 2021-08-02 | nan → default | - | - | - | -0.004325 |
| ALL | afl_100_zscore | -1 | 690615075 | 2021-09-01 | default → nan | - | - | - | +0.004304 |
| Top1000 | returns_zscore | -1 | 118522494 | 2020-03-13 | explicit → default | 0.000000 | 1.308596 | 0.000000 | +0.004247 |
| ALL | neg_returns_tsmean5_zscore | -1 | 102373675 | 2021-05-21 | nan → default | - | - | - | +0.004184 |
| Top1000 | afl_100_zscore | -1 | 249349939 | 2021-06-17 | default → nan | - | - | - | +0.004178 |
| ALL | neg_returns_tsmean5_zscore | -1 | 102373675 | 2021-06-07 | default → nan | - | - | - | -0.004169 |
| ALL | returns_zscore | -1 | 685581599 | 2021-08-27 | nan → default | - | - | - | -0.004102 |
| ALL | returns_zscore | -1 | 685581599 | 2021-09-13 | default → nan | - | - | - | +0.004084 |
| Top500 | returns_zscore | -1 | 12465726 | 2019-04-16 | default → nan | - | - | - | -0.004027 |
| Top500 | neg_returns_tsmean5_zscore | -1 | 2613257 | 2014-07-01 | default → computed | 0.000000 | 1.011055 | 0.002904 | -0.003925 |
| ALL | afl_100_zscore | -1 | 695175950 | 2021-08-03 | nan → default | - | - | - | -0.003917 |
| ALL | afl_100_zscore | -1 | 695175950 | 2021-09-01 | default → nan | - | - | - | +0.003912 |
| Top500 | afl_963_zscore | -1 | 12571130 | 2011-12-02 | default → computed | 0.000000 | 1.000000 | 0.000000 | +0.003631 |
| Top500 | neg_returns_tsmean5_zscore | -1 | 2651654 | 2012-03-28 | default → nan | - | - | - | +0.003571 |
| Top500 | neg_returns_tsmean5_zscore | -1 | 2651654 | 2012-03-27 | nan → default | - | - | - | -0.003571 |
| ALL | neg_returns_tsmean5_zscore | -1 | 1849973803 | 2025-09-04 | default → nan | - | - | - | -0.003490 |
| ALL | neg_returns_tsmean5_zscore | -1 | 1849973803 | 2025-09-03 | nan → default | - | - | - | +0.003490 |
| Top1000 | afl_963_zscore | -1 | 266372486 | 2022-10-12 | nan → default | - | - | - | +0.003471 |
| Top1000 | afl_963_zscore | -1 | 266372486 | 2022-10-13 | default → nan | - | - | - | -0.003471 |
| Top500 | neg_returns_tsmean5_zscore | -1 | 45835910 | 2018-05-23 | default → nan | - | - | - | -0.003247 |
| Top500 | afl_963_zscore | -1 | 45835910 | 2018-05-23 | default → nan | - | - | - | -0.003225 |
| Top500 | returns_zscore | -1 | 45835910 | 2018-05-23 | default → nan | - | - | - | -0.003206 |
| Top500 | returns_zscore | -1 | 2651654 | 2012-03-27 | nan → default | - | - | - | +0.003171 |
| Top500 | returns_zscore | -1 | 2651654 | 2012-03-28 | default → nan | - | - | - | -0.003171 |
| ALL | close_tsmean20_zscore | -1 | 34204761 | 2014-05-05 | computed → nan | - | - | - | -0.003056 |
| ALL | close_tsmean20_zscore | 0 | 34204761 | 2014-05-05 | computed → nan | - | - | - | -0.003056 |
| Top1000 | returns_zscore | -1 | 24993622 | 2025-08-07 | nan → default | - | - | - | +0.003010 |

(25256건 중 상위 40건만 표시; 전체는 changed_settlements.csv)
