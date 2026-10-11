"""Company names per ticker, so headlines that name the company ("Nvidia") match the symbol.

Covers the default universe, the alerts bot's top-20 list and the in-play scan's large caps.
Edit freely; a ticker without names still matches by symbol.
"""

COMPANY_NAMES: dict[str, tuple[str, ...]] = {
    "NVDA": ("Nvidia",), "AAPL": ("Apple",), "GOOGL": ("Alphabet", "Google"),
    "GOOG": ("Alphabet", "Google"), "MSFT": ("Microsoft",), "AMZN": ("Amazon",),
    "TSM": ("TSMC", "Taiwan Semiconductor"), "META": ("Meta Platforms", "Meta "),
    "AVGO": ("Broadcom",), "TSLA": ("Tesla",), "SPCX": ("SpaceX",), "MU": ("Micron",),
    "LLY": ("Eli Lilly", "Lilly"), "BRK.B": ("Berkshire",), "AMD": ("AMD", "Advanced Micro"),
    "JPM": ("JPMorgan",), "WMT": ("Walmart",), "V": ("Visa",), "XOM": ("Exxon",),
    "JNJ": ("Johnson & Johnson",), "INTC": ("Intel",), "MA": ("Mastercard",),
    "ABBV": ("AbbVie",), "ORCL": ("Oracle",), "COST": ("Costco",), "NFLX": ("Netflix",),
    "PLTR": ("Palantir",), "HD": ("Home Depot",), "PG": ("Procter",),
    "BAC": ("Bank of America",), "UNH": ("UnitedHealth",), "KO": ("Coca-Cola",),
    "CVX": ("Chevron",), "CRM": ("Salesforce",), "IBM": ("IBM",), "QCOM": ("Qualcomm",),
    "UBER": ("Uber",), "DIS": ("Disney",), "CSCO": ("Cisco",),
}
