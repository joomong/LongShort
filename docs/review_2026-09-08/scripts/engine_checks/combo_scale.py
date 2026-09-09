import numpy as np
rng=np.random.default_rng(5)
N=17; M=200000
# alpha A = <rank/> pct  -> values k/N, k=1..N (all positive)
# alpha B = <zscore/>    -> mean0 std1
A=np.tile((np.arange(1,N+1)/N),(M,1))
A=np.apply_along_axis(rng.permutation,1,A)
B=rng.standard_normal((M,N)); B=(B-B.mean(1,keepdims=True))/B.std(1,ddof=1,keepdims=True)
def book(s):
    w=s/np.abs(s).sum(1,keepdims=True)
    return w
for name,s in [("rank only",A),("zscore only",B),("rank+zscore (engine sum)",A+B)]:
    w=book(s)
    net=w.sum(1); gross=np.abs(w).sum(1)
    print(f"{name:26s} net exposure mean={net.mean():+.3f}  |net| mean={np.abs(net).mean():.3f}  long_ratio={ (w*(w>0)).sum(1).mean():.3f}")
# scale domination: raw close (BTC ~ 60000, others ~ 1..3000) vs zscore
px=np.concatenate([np.full((M,1),60000.),rng.uniform(1,3000,(M,N-1))],axis=1)
C=px-px.mean(1,keepdims=True)
w=book(C+B)
print(f"raw-close + zscore        max|w| mean={np.abs(w).max(1).mean():.3f}  (zscore-only={np.abs(book(B)).max(1).mean():.3f})")
