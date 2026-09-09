import numpy as np, pandas as pd, time, json
from kernel import sim_engine_loop, engine_pos, sim_team
t00=time.time()
d=np.load('panel.npz',allow_pickle=True); M=np.load('mask_slice.npy')
dates=pd.to_datetime(d['dates']); cols=d['cols']
close,hr,tr,vcr = d['close'], d['hr'], d['tr'], d['vcr']
ocr = close/d['open']-1.0   # OpenToCloseReturn 정의와 동일 (close/open-1)
T,N=close.shape
valid = M & np.isfinite(close)

def lag_ret(k):
    a=np.full_like(close,np.nan); a[k:]=close[k:]/close[:-k]-1.0; return a
m121=np.full_like(close,np.nan); m121[252:]=close[231:-21]/close[:-252]-1.0
alphas={'rev5':-lag_ret(5),'rev21':-lag_ret(21),'mom12_1':m121}

def xs_rank(a):
    R=np.full((T,N),np.nan); W=np.zeros((T,N))
    for t in range(T):
        m=valid[t]&np.isfinite(a[t]); n=int(m.sum())
        if n<20: continue
        r=pd.Series(a[t][m]).rank(method='average').to_numpy()/(n+1.0)-0.5
        R[t][m]=r; s=np.abs(r).sum()
        if s>1e-12: W[t][m]=r/s
    return R,W

ok=(dates>=pd.Timestamp('2015-01-01'))&(valid.sum(1)>0)&np.isfinite(hr).any(1)
ix=np.where(ok)[0]; sl=slice(ix[0],ix[-1]+1); nd=ix[-1]-ix[0]+1
yr=dates[sl].year.to_numpy()
def st(p):
    mu,sd=p.mean(),p.std(ddof=1); return mu*252, (mu/sd*np.sqrt(252) if sd>0 else np.nan)

L=[]; L.append('# 엔진 P&L 산식 vs 팀 규약 — trading leg 정량화\n')
L.append('- 데이터: `/home/base/cache/cache_v1_cash_1d/`, Top500 PIT 마스크 ∧ close 유한')
L.append('- 평가기간: **%s ~ %s, n=%d일**, 일평균 유효종목 %.1f개 (총 %s 셀)'%(
    dates[ix[0]].date(),dates[ix[-1]].date(),nd,valid[sl].sum()/nd,f"{valid[sl].sum():,}"))
L.append('- 패널 로드: 887개 security(2015 이후 Top500 편입 이력) × %d행, 9개 필드 3.1초\n'%T)

L.append('## 1. 수익률 정의 실측 검증 (valid 셀 1,335,283개)\n')
L.append('| 검증 대상 | 결과 |')
L.append('|---|---|')
L.append('| `HoldingReturn_Usd[t]` vs `close[t]/close[t-1]-1` | **완전 일치**, max&#124;diff&#124; = 0.0 (&#124;d&#124;>1e-6 비율 0.0000%) |')
L.append('| `TradingReturn_Usd[t]` vs `close[t]/midPrice[t]-1` | **완전 일치**, max&#124;diff&#124; = 0.0 |')
L.append('| `TradingReturn_Usd[t]` vs `midPrice[t]/close[t-1]-1` | 불일치 (99.94%가 &#124;d&#124;>1e-6) → 후보 기각 |')
L.append('| `VWAPToCloseReturn_Usd[t]` vs `close[t]/VWAP[t]-1` | **완전 일치**, max&#124;diff&#124; = 0.0 |')
L.append('| `midPrice` vs `(open+high+low+close)/4` | **완전 일치**, max&#124;diff&#124; = 2.3e-13 (부동소수점) |')
L.append('\n소스 확인: `/home/base/git/sim/modules/Returns.py` — `hr=hp.pct_change(fill_method="ffill")`, `tr=(hp/tp-1).fillna(0)`,')
L.append('`/home/base/git/sim/modules/BaseData_uss_v1.py:100` — `midPrice=(open+high+low+close)/4`. **[추정] 아님, 소스+실측 확정.**\n')
L.append('배당/분할 효과는 **없음**: HoldingReturn은 단순 종가비율이라 배당 미반영. 엔진의 close 비율과 정의상 동일.\n')
L.append('0-fill 함정 실측: valid 밖 `tr==0.0` 정확히 30.14%, `hr==0.0` 15.50% (valid 안은 각 0.15% / 0.47%).')
L.append('`tr` 컬럼 전체 NaN 비율 0.0% (fillna(0) 확인), `hr` 8.3%, `close` 16.4%.\n')
L.append('규모: valid 셀에서 `tr` mean=+7.6bp, std=0.886% (일간수익률 std 2.167%의 **0.41배**), corr(tr,hr)=0.781.\n')

L.append('## 2. 엔진 커널 재현 및 팀 규약 매핑\n')
L.append('`simulator.cpp` mode=1 을 `kernel.py:sim_engine_loop` 로 이식. 5×2 합성데이터 손계산 대조 max&#124;diff&#124;=0.0.')
L.append('- 확인된 구조: `target[di]=signal/Σ|signal|`, `exec_w=target[di-1]`, `prev_weights=target[di-2]`,')
L.append('  `ret[di]=Σ target[di-2]·(close[di]/close[di-1]-1) - Σ|target[di-1]-target[di-2]|·fee`')
L.append('- 첫 fee 부과 바 = **di=1** (turnover[0]=0, turnover[1]=1.0). di=0의 target은 버려짐.')
L.append('- **매핑: 엔진 `exec_w[t]` ≡ 팀 `pos[t]`** (즉 `pos[t]=target[t-1]`). 합성데이터에서 `sim_team(pos, cr, tr=0, cost=fee)` 와 엔진 결과 max&#124;diff&#124;=0.0 확인.')
L.append('- 실데이터 교차검증(fee를 1.5bp로 맞춘 엔진 vs T1): 일별 max&#124;diff&#124; ≈ 1.2e-3, 원인은 **12개 셀**(11.7년, 보유셀의 0.0009%)뿐.')
L.append('  전부 `close[t-1]=NaN`(1일 가격결측)인 종목 — 엔진은 그날 수익 전체를 0 처리, 팀은 hr의 ffill로 2일치 수익 인식. 누적 차이 -0.16bp/11.7년으로 무시가능.\n')
L.append('## 3. 알파별 결과\n')
L.append('알파: valid 마스크 내 cross-sectional rank pct - 0.5 → Σ&#124;pos&#124;=1. 정보시점 t-1 → pos[t] (look-ahead 없음).\n')
L.append('| 케이스 | hr | trading leg | 비용(편도) |')
L.append('|---|---|---|---|')
L.append('| E0 | close[t]/close[t-1]-1 | 없음 | 5.0bp |')
L.append('| E1 | HoldingReturn_Usd | 없음 | 5.0bp |')
L.append('| T0 | HoldingReturn_Usd | Δpos·TradingReturn_Usd | 1.5bp |')
L.append('| T1 | HoldingReturn_Usd | 없음 | 1.5bp |')
L.append('| T2 | HoldingReturn_Usd | Δpos·TradingReturn_Usd | 5.0bp |')
L.append('| T0v(참고) | HoldingReturn_Usd | Δpos·VWAPToCloseReturn_Usd (VWAP 체결) | 1.5bp |')
L.append('| T0o(참고) | HoldingReturn_Usd | Δpos·(close/open-1) (시가 체결) | 1.5bp |\n')
DEC=[]

EX={}
for name,a in alphas.items():
    R,W=xs_rank(a); pos=engine_pos(W)
    r_eng,tv_eng,tg=sim_engine_loop(R,close,5e-4); assert np.abs(tg-W).max()<1e-12
    E0=r_eng[sl]
    E1=sim_team(pos,hr,tr,5e-4,use_tr=False)[0][sl]
    p0,hold,trade,tvr=sim_team(pos,hr,tr,1.5e-4,use_tr=True)
    p1=sim_team(pos,hr,tr,1.5e-4,use_tr=False)[0]
    p2=sim_team(pos,hr,tr,5e-4,use_tr=True)[0]
    pv,_,tradev,_=sim_team(pos,hr,vcr,1.5e-4,use_tr=True)
    po,_,tradeo,_=sim_team(pos,hr,ocr,1.5e-4,use_tr=True)
    T0,T1,T2,T0v,T0o=p0[sl],p1[sl],p2[sl],pv[sl],po[sl]
    L.append('### %s (n=%d일, 일평균 turnover=%.3f)\n'%(name,nd,tvr[sl].mean()))
    L.append('| 케이스 | ann.return | Sharpe | vs T1 Sharpe |')
    L.append('|---|---|---|---|')
    s1=st(T1)[1]
    for tag,p in [('E0',E0),('E1',E1),('T0',T0),('T1',T1),('T2',T2),('T0v',T0v),('T0o',T0o)]:
        ar,sh=st(p); L.append('| %s | %+.2f%% | %.3f | %+.1f%% |'%(tag,100*ar,sh,100*(sh-s1)/abs(s1)))
    g=hold[sl].sum(); tl=trade[sl].sum(); tlv=tradev[sl].sum()
    L.append('')
    L.append('- gross P&L(Σ pos[t-1]·hr[t]) = %+.4f (연 %+.2f%%), **trading-leg 합계 = %+.4f (연 %+.2f%%p), gross 대비 %+.1f%%**'%(g,100*hold[sl].mean()*252,tl,100*trade[sl].mean()*252,100*tl/g))
    L.append('- 비용: 1.5bp → 연 -%.2f%%p, 5.0bp → 연 -%.2f%%p (차이 %.2f%%p)'%(100*tvr[sl].mean()*1.5e-4*252,100*tvr[sl].mean()*5e-4*252,100*tvr[sl].mean()*3.5e-4*252))
    L.append('- Sharpe: **trading leg 효과 T1→T0 = %.3f→%.3f (%+.1f%%)**, 비용 1.5→5bp 효과 T0→T2 = %.3f→%.3f (%+.1f%%)'%(
        s1,st(T0)[1],100*(st(T0)[1]-s1)/abs(s1),st(T0)[1],st(T2)[1],100*(st(T2)[1]-st(T0)[1])/abs(st(T0)[1])))
    L.append('- 엔진(E0) vs 팀(T0) Sharpe: %.3f → %.3f (%+.1f%%), ann %+.2f%% → %+.2f%%'%(st(E0)[1],st(T0)[1],100*(st(T0)[1]-st(E0)[1])/abs(st(E0)[1]) if st(E0)[1]!=0 else np.nan,100*st(E0)[0],100*st(T0)[0]))
    tlo=tradeo[sl].sum()
    L.append('- 체결가정 민감도 trading-leg 합계: midPrice %+.4f / VWAP %+.4f (%.2f배) / open %+.4f (%.2f배)'%(tl,tlv,tlv/tl if tl else np.nan,tlo,tlo/tl if tl else np.nan))
    L.append('- 회전 1단위당 trading-leg 엣지 = %.2fbp (합계/Σturnover)'%(1e4*tl/tvr[sl].sum()))
    DEC.append(dict(alpha=name,tvr=tvr[sl].mean(),E0=st(E0)[0],E1=st(E1)[0],T2=st(T2)[0],T0=st(T0)[0],
        shE0=st(E0)[1],shT1=st(T1)[1],shT0=st(T0)[1],shT2=st(T2)[1],
        d_hr=st(E1)[0]-st(E0)[0], d_tleg=st(T2)[0]-st(E1)[0], d_cost=st(T0)[0]-st(T2)[0],
        edge=1e4*tl/tvr[sl].sum(), ratio=tl/g))
    L.append('')
    L.append('연도별 trading-leg (P&L 단위, gross 대비 %):\n')
    ys=sorted(set(yr)); L.append('| 연도 | '+' | '.join(str(y) for y in ys)+' |')
    L.append('|---|'+'---|'*len(ys))
    L.append('| n일 | '+' | '.join(str(int((yr==y).sum())) for y in ys)+' |')
    L.append('| trading-leg | '+' | '.join('%+.4f'%trade[sl][yr==y].sum() for y in ys)+' |')
    L.append('| gross | '+' | '.join('%+.4f'%hold[sl][yr==y].sum() for y in ys)+' |')
    L.append('| Sharpe T1→T0 | '+' | '.join('%.2f→%.2f'%(st(T1[yr==y])[1],st(T0[yr==y])[1]) for y in ys)+' |')
    L.append('')
    # corr 진단
    dp=np.empty_like(pos); dp[0]=pos[0]; dp[1:]=pos[1:]-pos[:-1]
    m=valid[sl]&(np.abs(dp[sl])>1e-12)&np.isfinite(tr[sl])
    L.append('- 진단: 셀단위 corr(Δpos, tr) = %.4f (n=%s), Δpos·tr 평균 %+.2e'%(
        np.corrcoef(dp[sl][m],tr[sl][m])[0,1], f"{int(m.sum()):,}", (dp[sl]*tr[sl])[m].mean()))
    L.append('')
    EX[name]=dict(nd=int(nd),tvr=float(tvr[sl].mean()),gross=float(g),tleg=float(tl),
        sh={k:float(st(v)[1]) for k,v in [('E0',E0),('E1',E1),('T0',T0),('T1',T1),('T2',T2),('T0v',T0v),('T0o',T0o)]},
        ann={k:float(st(v)[0]) for k,v in [('E0',E0),('E1',E1),('T0',T0),('T1',T1),('T2',T2),('T0v',T0v),('T0o',T0o)]})
L.append('## 4. 엔진(E0) → 팀(T0) 격차 분해 (연율 %p, n=2936일)\n')
L.append('| 알파 | 일평균 turnover | E0 ann | ① hr교체 | ② trading leg | ③ 비용 5→1.5bp | T0 ann | 총격차 | leg 기여율 | 회전1단위 leg 엣지 |')
L.append('|---|---|---|---|---|---|---|---|---|---|')
for r in DEC:
    tot=r['T0']-r['E0']
    L.append('| %s | %.3f | %+.2f%% | %+.3f%%p | **%+.2f%%p** | %+.2f%%p | %+.2f%% | %+.2f%%p | %.0f%% | %.1fbp |'%(
        r['alpha'],r['tvr'],100*r['E0'],100*r['d_hr'],100*r['d_tleg'],100*r['d_cost'],100*r['T0'],100*tot,
        100*r['d_tleg']/tot if tot else np.nan, r['edge']))
L.append('')
L.append('| 알파 | Sharpe E0(엔진) | Sharpe T1(비용맞춤·leg없음) | Sharpe T0(팀) | leg의 Sharpe 효과 T1→T0 | 비용의 Sharpe 효과 T0→T2 |')
L.append('|---|---|---|---|---|---|')
for r in DEC:
    L.append('| %s | %.3f | %.3f | %.3f | **%+.1f%%** | %+.1f%% |'%(r['alpha'],r['shE0'],r['shT1'],r['shT0'],
        100*(r['shT0']-r['shT1'])/abs(r['shT1']), 100*(r['shT2']-r['shT0'])/abs(r['shT0'])))
L.append('')
L.append('## 5. 결론\n')
L.append('**Q. trading leg 누락, 상관 있나 → 회전율에 비례해서 상관 있다. 다만 비용 가정 차이가 더 크다.**\n')
L.append('1. **trading leg 크기 = 회전율 × 약 0.2~0.9bp.** 회전 1단위당 엣지가 rev5 0.9bp / rev21 0.5bp / mom12_1 0.2bp 로,')
L.append('   알파의 *당일* 예측력에 비례. 절대 크기는 편도비용 1.5bp보다 작지만 무시할 수준은 아님.')
L.append('2. **Sharpe 왜곡(같은 비용 기준, T1 vs T0)**: 엔진은 팀 대비 rev5 -38.8%, rev21 -20.7%, mom12_1 -1.6% 낮게 본다.')
L.append('   즉 **일평균 turnover가 0.3을 넘는 고회전 알파에서는 엔진이 20~40% 보수적**으로 나오고, turnover 0.08 수준의 저회전 알파는 사실상 무관.')
L.append('3. **부호는 3종 모두 (+)** — 직관("리버설은 체결에서 손해")과 반대. 이유: 팀 규약의 pos[t]는 t-1 정보로 만들어 midPrice[t]=(O+H+L+C)/4 에 체결되므로,')
L.append('   신규 체결분 Δpos[t]가 **당일 t의 midPrice→close 구간 알파를 추가로 먹는다**. 엔진은 close[t]에 체결하므로 이 구간을 통째로 포기.')
L.append('   단기 리버설은 당일 반등이 가장 강해서 이 구간 엣지가 가장 크고(rev5), 12-1 모멘텀은 당일 인트라데이 예측력이 거의 없어 leg≈0.')
L.append('   연도별로도 rev5는 12년 중 10년 (+), rev21은 7년 (+) 으로 부호가 안정적(2022 rev21 -0.0214 가 최대 예외).')
L.append('4. **leg 크기는 체결가 가정에 크게 의존** (leg 합계, midPrice / VWAP / open 순):')
L.append('   rev5 +0.157 / +0.270(1.72배) / +0.100(0.64배) — 세 가정 모두 (+).')
L.append('   rev21 +0.042 / +0.109(2.58배) / -0.001(소멸) — 시가 체결이면 leg 없음.')
L.append('   mom12_1 +0.005 / -0.000 / +0.008 — 어느 쪽이든 ~0.')
L.append('   즉 **부호는 리버설에서 안정적으로 (+) 지만 크기는 0~2.6배로 흔들림**. midPrice=(O+H+L+C)/4 는 사후적 H/L 을 쓰는 벤치마크라')
L.append('   실집행 관점에서는 VWAP 쪽이 현실적. [추정] 시가 체결이 가장 나쁜 건 야간 갭에서 리버설이 이미 상당부분 소진되기 때문.')
L.append('5. **close 비율 vs HoldingReturn_Usd 차이는 사실상 0** (연 +0.005~0.015%p). HoldingReturn은 정의상 종가비율 그대로라 배당/코액 효과 없음.')
L.append('   엔진의 close 패널을 그대로 써도 P&L 왜곡 없음. 유일한 차이는 close[t-1]=NaN 인 11.7년 통틀어 12개 셀.')
L.append('6. **정작 더 큰 건 비용 가정.** 엔진 config(`/home/jgo/longshort_platform/config.yaml: transaction_fee: 5`)의 편도 5bp vs 팀 1.5bp 차이가')
L.append('   rev5에서 연 4.98%p (전체 격차의 79%) 로 trading leg(1.35%p, 21%)의 3.7배. rev5 Sharpe는 비용만 5bp로 바꿔도 0.475→-0.191 로 부호가 뒤집힘.')
L.append('   크립토 5bp 를 미국주식에 그대로 쓰면 고회전 알파를 전부 죽인다.\n')
L.append('**권고**: (a) fee를 1.5bp(팀 규약)로 맞추는 것이 1순위, (b) trading leg는 midPrice 패널을 하나 더 로드해')
L.append('`ret[di] += Σ (exec_w-prev_weights)·(close[di]/midPrice[di]-1)` 한 줄 추가로 재현 가능 —')
L.append('엔진 `exec_w[t]`가 팀 `pos[t]`와 정확히 같은 자리라 인덱스 변경 없이 붙는다.')
L.append('(c) 저회전(turnover<0.1) 알파만 볼 거면 현재 엔진 그대로도 무방.\n')
json.dump(EX,open('summary.json','w'),indent=1)
open('RESULT.md','w').write('\n'.join(L)+'\n')
print('\n'.join(L[-0:]) if False else 'written %.1fs'%(time.time()-t00))
print(json.dumps(EX,indent=1))
