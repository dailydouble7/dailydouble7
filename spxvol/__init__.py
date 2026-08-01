"""spxvol -- implied vol and rich-trade detection for SPX options from bar data.

Recovers the forward and discount from put-call parity (no index feed, no rate
curve, no dividend forecast), inverts Black-76 for implied vol, fits a smile per
timestamp, and scores large prints against a leave-one-out fit.
"""

from .black76 import forward_delta, implied_vol, price, vega
from .forward import ParityQuote, solve_forward
from .occ import OptionContract, decode, encode
from .signal import FlaggedTrade, SkewHistory, evaluate_print
from .smile import SmileFit, fit_smile, make_point

__version__ = "0.1.0"

__all__ = [
    "FlaggedTrade",
    "OptionContract",
    "ParityQuote",
    "SkewHistory",
    "SmileFit",
    "decode",
    "encode",
    "evaluate_print",
    "fit_smile",
    "forward_delta",
    "implied_vol",
    "make_point",
    "price",
    "solve_forward",
    "vega",
]
