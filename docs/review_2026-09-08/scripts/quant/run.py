import numpy as np, pandas as pd, time, json
from kernel import sim_engine_loop, engine_pos, sim_team
t00=time.time()
d=np.load('panel.npz',allow_pickle=True); M=np.load('mask_slice.npy')
dates=pd.to_datetime(d['dates']); cols=d['cols']
close,hr,tr = d['close'], d['hr'], d['tr']
T,N=close.shape
valid = M & np.isfinite(close)                      # Top500 PIT ∧ close 유한
cr = np.full_like(close,np.nan); cr[1:]=close[1:]/close[:-1]-1.0

# ---- 알파(정보시점 t) : pos[t+1]=target[t] 로 쓰이므로 pos 는 t-1 정보 ----
def lag_ret(k):
    a=np.full_like(close,np.nan); a[k:]=close[k:]/close[:-k]-1.0; return a
alphas={
 'rev5  : -(C/C[-5]-1)'      : -lag_ret(5),
 'rev21 : -(C/C[-21]-1)'     : -lag_ret(21),
 'mom12_1: C[-21]/C[-252]-1' : np.vstack([np.full((21,N),np.nan), (close[21-1:-1][ (0,) ] if False else np.full((0,N),np.nan))]) # placeholder
}
m121=np.full_like(close,np.nan)
m121[252:] = close[252-21:-21]/close[:-252]-1.0      # close[t-21]/close[t-252]-1
alphas['mom12_1: C[-21]/C[-252]-1']=m121

def xs_rank(a):
    """valid ∧ finite 셀에서 cross-sectional rank pct - 0.5 (엔진에 넣는 raw signal).
    R: 마스크 밖 NaN,  W: R/sum|R| (= 엔진 target)"""
    R=np.full((T,N),np.nan); W=np.zeros((T,N))
    for t in range(T):
        m=valid[t] & np.isfinite(a[t])
        n=int(m.sum())
        if n<20: continue
        v=a[t][m]
        r=pd.Series(v).rank(method='average').to_numpy()/(n+1.0) - 0.5
        R[t][m]=r
        s=np.abs(r).sum()
        if s>1e-12: W[t][m]=r/s
    return R,W

EV0=pd.Timestamp('2015-01-01')
ok_row = (dates>=EV0) & np.isfinite(hr).any(1) & (valid.sum(1)>0)
ev=np.where(ok_row)[0]; e0,e1=ev[0],ev[-1]
print('eval window %s ~ %s  n=%d'%(dates[e0].date(),dates[e1].date(),e1-e0+1))
sl=slice(e0,e1+1); ndays=e1-e0+1
yr=dates[sl].year.to_numpy()

def stats(p):
    mu=p.mean(); sd=p.std(ddof=1)
    return mu*252, mu/sd*np.sqrt(252) if sd>0 else np.nan
rows=[]; extra={}
for name,a in alphas.items():
    R,W=xs_rank(a)                               # raw rank signal / target[t]
    pos=engine_pos(W)                            # pos[t]=target[t-1]  (=엔진 exec_w[t])
    # E0: 엔진 커널 (close 비율, trading leg 없음, fee 5bp)
    sig=R                                        # 엔진에 넣는 signal(마스크 밖 NaN)
    # 유니버스 제약은 signal=NaN 으로 들어가므로 close 는 원본 그대로 (엔진 실제 배선)
    r_eng,tv_eng,tgt_eng=sim_engine_loop(sig, close, 5e-4)
    # 참고: close 까지 유니버스 마스킹한 변형(=탈락종목 수익 누락)
    r_engM,_,_=sim_engine_loop(sig, np.where(valid, close, np.nan), 5e-4)
    assert np.abs(tgt_eng-W).max()<1e-12, 'target 불일치'
    E0=r_eng[sl]
    # E1: hr = HoldingReturn_Usd 로 교체
    e1p,_,_,_=sim_team(pos, hr, np.zeros_like(tr), 5e-4, use_tr=False); E1=e1p[sl]
    # T0/T1/T2
    t0p,hold,trade,tvr=sim_team(pos, hr, tr, 1.5e-4, use_tr=True)
    t1p,_,_,_        =sim_team(pos, hr, tr, 1.5e-4, use_tr=False)
    t2p,_,_,_        =sim_team(pos, hr, tr, 5e-4,  use_tr=True)
    T0,T1,T2=t0p[sl],t1p[sl],t2p[sl]
    # 엔진 fee 를 1.5bp 로 낮추면 T1 과 동일해야 함(교차검증)
    r_eng15,_,_=sim_engine_loop(sig, close, 1.5e-4)
    chk=np.abs(r_eng15[sl]-T1).max()
    for tag,p in [('E0',E0),('E1',E1),('T0',T0),('T1',T1),('T2',T2)]:
        ar,sh=stats(p); rows.append(dict(alpha=name,case=tag,ann=ar,sharpe=sh,tvr=tvr[sl].mean(),n=ndays))
    g=hold[sl].sum(); tl=trade[sl].sum()
    extra[name]=dict(check_E0vsT1=float(chk), gross=float(g), tleg=float(tl), ratio=float(tl/g),
        tleg_ann=float(trade[sl].mean()*252), hold_ann=float(hold[sl].mean()*252),
        cost15=float(tvr[sl].mean()*1.5e-4*252), cost50=float(tvr[sl].mean()*5e-4*252),
        E0_vs_E1_maxdiff=float(np.abs(E0-E1).max()),
        E0_vs_E1_sumdiff=float((E0-E1).sum()),
        maskclose_ann=float(r_engM[sl].mean()*252), maskclose_sh=float(stats(r_engM[sl])[1]),
        yearly={int(y): dict(tleg=float(trade[sl][yr==y].sum()), hold=float(hold[sl][yr==y].sum()),
                             sh_T0=float(stats(T0[yr==y])[1]), sh_T1=float(stats(T1[yr==y])[1]),
                             n=int((yr==y).sum())) for y in np.unique(yr)})
    print('%-28s done  E0-vs-T1(fee1.5bp) max|d|=%.2e  E0-vs-E1 max|d|=%.2e  %.1fs'%(name,chk,np.abs(E0-E1).max(),time.time()-t00))
df=pd.DataFrame(rows)
df.to_csv('results.csv',index=False); json.dump(extra,open('extra.json','w'),indent=1)
pd.set_option('display.width',200)
print(df.pivot(index='alpha',columns='case',values=['ann','sharpe']).round(4))
print('total %.1fs'%(time.time()-t00))
