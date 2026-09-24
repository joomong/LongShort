# Xpressfeed 로더 스케줄 변경 요청 — 기업행사·식별자 이력 패키지

대상 시스템: xfeed(localhost PostgreSQL) Xpressfeed Loader, 제어 스키마 `xfl_control`. 현상·근거는 [xfeed_source_findings_2026-09-11.md §7](xfeed_source_findings_2026-09-11.md).

## 현상
- `xfl_target.co_acthist`·`sec_idhist`가 2026-02-19 이후 미적재. S&P 전송은 정상(`/Xpressfeed/cana`, `/Xpressfeed/caintntl`, `caref`, `caprihistxna`, `secidtichist`, `secidunlicisinhist`에 2026-09-10 파일까지 도착, `xfu_actualzipfile.currentlyavailableflag=1`).
- 해당 적재 구독이 1회성 크론 스케줄에만 배정돼 있음.

## 요청

| loadsub | 패키지(pacvertofeedpop) | 현재 schedule | 요청 |
|---|---|---|---|
| 718 | Corporate Actions NA (38) → `co_acthist` | 737 `Meritz_Schedule2` = `0 0 23 31 1 ? 2025` | **22 `every 1hour`**(가격·AFL과 동일) 또는 일 1회 스케줄 신설 |
| 733 | Corporate Actions International (8998) → `co_acthist` | 737 | 동일 |
| 720 | Corporate Actions Primary Hist Ex NA (39) → `sec_idhist` | 737 | 동일 |
| 729 | Security Identifiers TIC History (8959) → `sec_idhist` | 741 `Meritz_Schedule4` = `0 0 23 1 2 ? 2025` | 동일 |
| 731 | Security Identifiers Unlicensed ISIN History (8960) → `sec_idhist` | 741 | 동일 |

참고: 파일은 일별 전체 스냅샷(co_acthist ~2.4MB, 121k행)이라 최신 파일 1회 적재로 공백이 메워짐. Gap 검사(`ignoregapcheckflag`)가 막으면 해당 구독만 gap 무시 후 재실행.

## 적용 후 확인 (읽기 전용)
```sql
select max(datadate) from xfl_target.co_acthist;                       -- 2026-09-1x 기대
select loadsubid, scheduleid, loadsubenddatetime, loadsuberrors from xfl_control.xfu_loadsub where loadsubid in (718,720,729,731,733);
select actualzipfilename, lastdownloadosdatetime from xfl_control.xfu_actualzipfile where zipfiledirectory in ('/Xpressfeed/cana','/Xpressfeed/caintntl') order by zipfiledate desc limit 4;
```
