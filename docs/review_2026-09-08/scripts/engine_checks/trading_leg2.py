import numpy as np
rng=np.random.default_rng(11)
T,N=6000,17

def run(phi, halflife, kind, label):
    e=rng.standard_normal((T,N))*0.04
    r=np.zeros((T,N))
    for t in range(1,T): r[t]=phi*r[t-1]+e[t]
    r-=r.mean(axis=1,keepdims=True)
    close=np.cumprod(1+r,axis=0)*100
    hr=np.vstack([np.zeros((1,N)), close[1:]/close[:-1]-1])
    mid=np.vstack([close[0:1], np.sqrt(close[1:]*close[:-1])])
    tr=close/mid-1
    sign=-1.0 if kind=="reversal" else 1.0
    # smoothed signal -> lower turnover (EWMA of past returns, halflife days)
    lam=0.5**(1.0/halflife)
    s=np.zeros((T,N)); acc=np.zeros(N)
    for t in range(T):
        acc=lam*acc+(1-lam)*r[t]; s[t]=sign*acc
    def norm(x):
        a=np.abs(x).sum(axis=1,keepdims=True); a[a<1e-12]=np.inf
        return x/a
    tgt=norm(s)
    pos=np.vstack([np.zeros((1,N)), tgt[:-1]])       # info <= t-1  (team pos[t])
    dpos=np.vstack([pos[0:1], np.diff(pos,axis=0)])
    hold=np.array([(pos[t-1]*hr[t]).sum() if t>0 else 0. for t in range(T)])
    trade=np.array([(dpos[t]*tr[t]).sum() for t in range(T)])
    tvr=np.abs(dpos).sum(axis=1)
    fee=5e-4
    cost=tvr*fee
    ann=lambda x:x.mean()*365*100
    shp=lambda x:x.mean()/x.std(ddof=1)*np.sqrt(365)
    eng=hold-cost; tm=hold+trade-cost
    print(f"{label:26s} tvr={tvr.mean():.3f} | engine(net) ann={ann(eng):6.2f}% shp={shp(eng):5.2f} "
          f"| team(net) ann={ann(tm):6.2f}% shp={shp(tm):5.2f} | trade-leg ann={ann(trade):6.2f}% "
          f"= {ann(trade)/abs(ann(eng))*100:6.1f}% of engine")
for hl in (1,3,10):
    for phi,kind in ((-0.03,"reversal"),(0.03,"momentum")):
        run(phi,hl,kind,f"hl={hl}d {kind} phi={phi:+.2f}")
