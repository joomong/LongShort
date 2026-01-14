from framework.modules import (
    Price,
    SMA,
    Rank,
    ConstantSignal,
    ZScore,
    tszscore,
    tsrank,
    normalize_by_symbol,
)

returns = tszscore((Price("close") - Price("open")) / Price("close"), window=5)
final_alpha = normalize_by_symbol(returns)
