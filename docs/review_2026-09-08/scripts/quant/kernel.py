"""cpp-engine simulator.cpp (mode=1) 파이썬 재현 + 팀 규약 StatsHandler 재현."""
import numpy as np

def sim_engine_loop(signal, close, fee):
    """simulator.cpp run_from_signals(), mode=1 을 그대로 옮긴 스칼라 루프."""
    T, N = signal.shape
    ret = np.zeros(T); tvr_o = np.zeros(T)
    prev_weights = np.zeros(N); prev_target = np.zeros(N); target = np.zeros(N)
    tgt_hist = np.zeros((T, N))
    for di in range(T):
        close_row = close[di]
        ok = np.isfinite(close_row) & np.isfinite(signal[di])
        abs_sum = np.abs(signal[di][ok]).sum()
        target = np.zeros(N)
        if abs_sum > 1e-12:
            target[ok] = signal[di][ok] / abs_sum
        tgt_hist[di] = target
        exec_w = prev_target                      # mode==1
        if di > 0:
            pc = close[di-1]
            m = (pc > 0) & (close_row > 0)
            ret[di] += float((prev_weights[m] * (close_row[m]/pc[m] - 1.0)).sum())
        tvr = float(np.abs(exec_w - prev_weights).sum())
        tvr_o[di] = tvr
        ret[di] -= tvr * fee
        prev_weights, prev_target, target = prev_target.copy(), target.copy(), prev_weights
    return ret, tvr_o, tgt_hist

def engine_pos(target):
    """엔진 exec_w[t] = target[t-1]  (팀 규약의 pos[t] 와 동일 매핑)."""
    pos = np.zeros_like(target); pos[1:] = target[:-1]
    return pos

def sim_team(pos, hr, tr, cost, use_tr=True):
    """StatsHandler.run(): pnl[t] = sum( pos[t-1]*hr[t] + dpos[t]*tr[t] - |dpos[t]|*cost )."""
    pos = np.nan_to_num(pos, nan=0.0, posinf=0.0, neginf=0.0)
    hr = np.where(np.isfinite(hr), hr, 0.0)
    tr = np.where(np.isfinite(tr), tr, 0.0)
    dpos = np.empty_like(pos); dpos[0] = pos[0]; dpos[1:] = pos[1:] - pos[:-1]
    sp = np.empty_like(pos); sp[0] = 0.0; sp[1:] = pos[:-1]
    hold = (sp * hr).sum(1)
    trade = (dpos * tr).sum(1) if use_tr else np.zeros(len(pos))
    tvr = np.abs(dpos).sum(1)
    return hold + trade - tvr * cost, hold, trade, tvr
