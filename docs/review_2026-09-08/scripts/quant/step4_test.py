import numpy as np
from kernel import sim_engine_loop, engine_pos, sim_team
np.set_printoptions(precision=6, suppress=True)
sig = np.array([[1.,-1.],[2.,-1.],[1.,1.],[-1.,-3.],[0.5,-0.5]])
close = np.array([[10.,20.],[11.,19.],[10.5,20.],[12.,21.],[11.,22.]])
fee = 0.0005
ret,tvr,tgt = sim_engine_loop(sig, close, fee)
print('target rows (signal/sum|signal|):'); print(tgt)
print('turnover:',tvr); print('ret     :',ret)
# 손계산
cr = np.vstack([np.full((1,2),np.nan), close[1:]/close[:-1]-1])
man=np.zeros(5)
for t in range(5):
    pw = tgt[t-2] if t>=2 else np.zeros(2)
    ew = tgt[t-1] if t>=1 else np.zeros(2)
    r = (pw*cr[t]).sum() if t>0 else 0.0
    man[t] = r - np.abs(ew-pw).sum()*fee
print('manual  :',man,' max|diff|=%.3e'%np.abs(man-ret).max())
print('첫 fee 부과 bar =', int(np.argmax(tvr>0)), ' turnover[0]=',tvr[0],' turnover[1]=',tvr[1])
# 엔진 <-> 팀 매핑 동치 (tr=0, 같은 비용)
pos = engine_pos(tgt)
pnl,_,_,tvr_t = sim_team(pos, cr, np.zeros_like(cr), fee, use_tr=False)
print('team(pos=target[t-1], tr=0, cost=fee):',pnl,' max|diff vs engine|=%.3e'%np.abs(pnl-ret).max())
print('turnover 일치 max|d|=%.3e'%np.abs(tvr_t-tvr).max())
