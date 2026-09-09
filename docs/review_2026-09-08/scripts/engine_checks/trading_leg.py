import numpy as np
rng=np.random.default_rng(7)
T,N=4000,17
def panel(phi):
    # cross-sectional demeaned returns with AR(1) coef phi
    e=rng.standard_normal((T,N))*0.04
    r=np.zeros((T,N))
    for t in range(1,T): r[t]=phi*r[t-1]+e[t]
    r-=r.mean(axis=1,keepdims=True)
    return r

def run(phi, kind):
    r=panel(phi)
    close=np.cumprod(1+r,axis=0)*100
    hr=np.vstack([np.zeros((1,N)), close[1:]/close[:-1]-1])
    mid=np.vstack([close[0:1], np.sqrt(close[1:]*close[:-1])])   # intraday mid proxy
    tr=close/mid-1
    sign=-1.0 if kind=="reversal" else 1.0
    raw=sign*r                                   # signal at bar t uses r[t] (info <= t)
    def norm(s):
        a=np.abs(s).sum(axis=1,keepdims=True); a[a<1e-12]=np.inf
        return s/a
    tgt=norm(raw)
    # team convention: pos[t] uses info<=t-1  => pos[t]=tgt[t-1]
    pos=np.vstack([np.zeros((1,N)), tgt[:-1]])
    dpos=np.vstack([pos[0:1], np.diff(pos,axis=0)])
    hold=np.array([ (pos[t-1]*hr[t]).sum() if t>0 else 0.0 for t in range(T)])
    trade=np.array([ (dpos[t]*tr[t]).sum() for t in range(T)])
    tvr=np.abs(dpos).sum(axis=1)
    ann=lambda x: x.mean()*365
    shp=lambda x: x.mean()/x.std(ddof=1)*np.sqrt(365)
    print(f"phi={phi:+.2f} {kind:9s}  hold ann={ann(hold)*100:7.2f}%% sharpe={shp(hold):5.2f} | "
          f"trade-leg ann={ann(trade)*100:7.2f}%% ({ann(trade)/abs(ann(hold))*100:6.1f}% of hold) | "
          f"with-leg sharpe={shp(hold+trade):5.2f} | mean tvr={tvr.mean():.3f}")
for phi in (-0.10, 0.0, 0.10):
    for k in ("reversal","momentum"):
        run(phi,k)
