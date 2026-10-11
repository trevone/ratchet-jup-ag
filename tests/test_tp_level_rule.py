from dca_levels import smart_tps, Levels, SmartTpParams, LONG
SHORT = -LONG

P = SmartTpParams(res_buffer_atr=0.1, min_net_usd=0.05, min_gap_usd=0.05, market_gap=0.02)


def test_level_mode_long_front_runs_nearest_level_and_tp2_ratio():
    lv = Levels(supports=(80000.0,), resistances=(84000.0,), atr=100.0, fine_resistances=(83300.0, 82970.0))
    r = smart_tps(LONG, 82960.23, 200.0, [83300.0, 83580.0], [0.5, 1.0], 0, lv, P, market=82900.0,
                  tp1_level=True, tp2_from_tp1_ratio=0.6)
    # 82970 - 10 = 82960 is inside the fee floor -> next level out (fine 83300)
    assert abs(r.prices[0] - 83290.0) < 1e-6
    assert abs(r.prices[1] - (82960.23 + (83290.0 - 82960.23) / 0.6)) < 1e-6


def test_level_mode_short_mirrored():
    lv = Levels(supports=(2400.0,), resistances=(2600.0,), atr=10.0, fine_supports=(2450.0,))
    r = smart_tps(SHORT, 2484.0, 100.0, [2470.0, 2460.0], [0.5, 1.0], 0, lv, P, market=2480.0,
                  tp1_level=True, tp2_from_tp1_ratio=0.6)
    assert abs(r.prices[0] - 2451.0) < 1e-6
    assert abs(r.prices[1] - (2484.0 - 33.0 / 0.6)) < 1e-6
