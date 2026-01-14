from framework.modules import *

ibs = (Price("close") - Price("low")) / (Price("high") - Price("low"))

final_alpha = NormalizeBySymbol(tszscore(ibs, window=20), mode="zscore")
