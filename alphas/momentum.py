from framework.modules import Price, SMA, Rank , ConstantSignal , ZScore

# 1. 기본 재료(시그널) 정의
# close = Price('close')


ibs = -1* (Price('close') - Price('low')) / (Price('open') - Price('low'))



final_alpha = ZScore(ibs)