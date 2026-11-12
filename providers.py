import gw_paytm as paytm, gw_fampay as fampay
ADAPTERS = {"paytm": paytm, "fampay": fampay}
def get(name): return ADAPTERS.get(name)
