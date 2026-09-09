import numpy as np
np.random.seed(0)
T, N = 12, 3
close = np.cumprod(1+np.random.randn(T,N)*0.02, axis=0)*100

def engine(signal, mode=1, fee=0.0):
    """replicate simulator.cpp run()"""
    prev_w = np.zeros(N); prev_t = np.zeros(N)
    ret=np.zeros(T); tvr=np.zeros(T); execw_log=[]; prevw_log=[]
    for di in range(T):
        s = signal[di]
        mask = np.isfinite(close[di]) & np.isfinite(s)
        a = np.abs(s[mask]).sum()
        tgt = np.zeros(N)
        if a > 1e-12: tgt[mask] = s[mask]/a
        exec_w = tgt if mode==0 else prev_t
        r = 0.0
        if di>0:
            ok = (close[di-1]>0)&(close[di]>0)
            r = float((prev_w[ok]*(close[di][ok]/close[di-1][ok]-1)).sum())
        t = float(np.abs(exec_w-prev_w).sum())
        ret[di]=r - t*fee; tvr[di]=t
        execw_log.append(exec_w.copy()); prevw_log.append(prev_w.copy())
        if mode==1:
            prev_w, prev_t, tgt2 = prev_t.copy(), tgt.copy(), None
        else:
            prev_w = tgt.copy()
    return ret, tvr, np.array(execw_log), np.array(prevw_log)

def team(pos, fee=0.0, tr=None):
    """pnl[t] = pos[t-1]*hr[t] + dpos[t]*tr[t] - |dpos|*cost ; pos[t] uses data<=t-1"""
    hr = np.vstack([np.full(N,np.nan), close[1:]/close[:-1]-1])
    if tr is None: tr = np.zeros((T,N))   # execution at close -> tr=0
    dpos = np.vstack([pos[0:1], np.diff(pos,axis=0)])
    pnl=np.zeros(T)
    for t in range(T):
        h = 0.0 if t==0 else float((pos[t-1]*hr[t]).sum())
        pnl[t]= h + float((dpos[t]*tr[t]).sum()) - float(np.abs(dpos[t]).sum())*fee
    return pnl

# raw signal: use close itself, no XML delay -> signal[di] = f(close[di])
raw = close - close.mean(axis=1, keepdims=True)
sig_nodelay = raw.copy()
sig_delay1  = np.vstack([np.full((1,N),np.nan), raw[:-1]])   # MDelay(days=1)

for name, sig in [("delay=0", sig_nodelay), ("delay=1", sig_delay1)]:
    ret, tvr, ew, pw = engine(sig, mode=1, fee=0.0)
    # normalized targets
    def norm(s):
        out=np.zeros((T,N))
        for di in range(T):
            m=np.isfinite(close[di])&np.isfinite(s[di]); a=np.abs(s[di][m]).sum()
            if a>1e-12: out[di][m]=s[di][m]/a
        return out
    tgt = norm(sig)
    # team pos[t] such that pos[t-1] earns hr[t]  ->  pos[t] = engine prev_weights at t+1 = tgt[t-1]
    pos_team_equiv = np.vstack([np.zeros((1,N)), tgt[:-1]])   # pos[t]=tgt[t-1]  (info <= t-1 when delay=0)
    p = team(pos_team_equiv, fee=0.0)
    print(name, "max|engine-team_equiv| =", np.nanmax(np.abs(ret-p)))
    print("   engine prev_weights[t] == tgt[t-2]? ", np.allclose(pw[2:], tgt[:-2]))
