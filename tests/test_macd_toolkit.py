"""
Pruebas de macd_toolkit.py   ->   python -m pytest -q

Cada prueba verifica una propiedad matematica que se explica en la guia:
recursion de la EMA, equivalencia con pandas, retraso (N-1)/2 en una
tendencia lineal, MACD ~ 7 * pendiente, arranque de la senal en t = 34
y el RSI de la clase (RS = 3.6 -> RSI = 78.26).
"""

import numpy as np
import pandas as pd
import pytest

import macd_toolkit as mt


def toy():
    idx = pd.date_range("2026-01-02", periods=8, freq="W-FRI")
    return pd.Series([10, 11, 12, 11, 13, 14, 13, 15], index=idx, dtype=float)


def random_walk(n=300, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, n))), index=idx)


def test_alpha():
    assert mt.alpha_from_span(12) == pytest.approx(2 / 13)
    assert mt.alpha_from_span(26) == pytest.approx(2 / 27)
    assert mt.alpha_from_span(9) == pytest.approx(0.2)


def test_sma_toy():
    s = mt.sma(toy(), 3)
    assert s.iloc[:2].isna().all()
    assert s.iloc[2] == pytest.approx(11.0)
    assert s.iloc[-1] == pytest.approx((14 + 13 + 15) / 3)


def test_ema_toy_by_hand():
    # N = 3 -> alpha = 0.5 ; semilla = SMA de los primeros 3 = 11
    e = mt.ema(toy(), 3)
    expected = [np.nan, np.nan, 11.0, 11.0, 12.0, 13.0, 13.0, 14.0]
    np.testing.assert_allclose(e.to_numpy(), expected, equal_nan=True)


def test_ema_first_seed_equals_pandas():
    x = random_walk()
    for n in (3, 9, 12, 26):
        mine = mt.ema(x, n, seed="first")
        ref = x.ewm(span=n, adjust=False).mean()
        np.testing.assert_allclose(mine.to_numpy(), ref.to_numpy(), rtol=0, atol=1e-10)


def test_seed_effect_decays():
    x = random_walk()
    a = mt.ema(x, 26, seed="sma")
    b = mt.ema(x, 26, seed="first")
    gap = (a - b).abs().dropna()
    # la diferencia inicial se multiplica por (1 - alpha) en cada paso
    ratio = gap.iloc[11] / gap.iloc[10]
    assert ratio == pytest.approx(1 - 2 / 27, rel=1e-9)
    assert gap.iloc[-1] < 1e-3 * gap.iloc[0]


def test_lag_on_linear_trend():
    t = np.arange(400, dtype=float)
    x = pd.Series(50 + 0.3 * t)
    for n in (12, 26):
        lag = (n - 1) / 2
        assert (x - mt.sma(x, n)).iloc[-1] == pytest.approx(0.3 * lag)
        assert (x - mt.ema(x, n)).iloc[-1] == pytest.approx(0.3 * lag, rel=1e-6)


def test_macd_is_seven_times_slope():
    t = np.arange(400, dtype=float)
    m = 0.3
    df = mt.macd(pd.Series(50 + m * t))
    assert df["macd"].iloc[-1] == pytest.approx(7 * m, rel=1e-6)
    assert df["hist"].iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_warmup_positions():
    x = random_walk(60)
    df = mt.macd(x)
    assert df["macd"].first_valid_index() == x.index[25]    # t = 26
    assert df["signal"].first_valid_index() == x.index[33]  # t = 34
    cfg = mt.MACDConfig()
    assert (cfg.first_macd, cfg.first_signal) == (26, 34)


def test_signal_seed_is_sma_of_first_nine_macd():
    x = random_walk(80)
    df = mt.macd(x)
    first_nine = df["macd"].dropna().iloc[:9].mean()
    assert df["signal"].dropna().iloc[0] == pytest.approx(first_nine)


def test_sma_variant_runs():
    df = mt.macd(random_walk(80), mt.MACDConfig(kind="sma"))
    assert df["signal"].first_valid_index() is not None


def test_crossovers_detects_signs():
    df = mt.macd(random_walk(300))
    ev = mt.crossovers(df)
    assert set(ev["evento"]) <= {"cruce_alcista", "cruce_bajista", "cero_arriba", "cero_abajo"}
    for _, row in ev.iterrows():
        if row.evento == "cruce_alcista":
            assert row.macd > row.signal
        if row.evento == "cruce_bajista":
            assert row.macd < row.signal


def test_cruces_ignoran_ruido_de_redondeo_en_tendencia_lineal():
    # En una recta el histograma converge a cero y solo queda ruido de ~1e-15
    # que cambia de signo. Antes eso producia un cruce bajista falso.
    idx = pd.bdate_range("2025-01-01", periods=200)
    df = mt.macd(pd.Series(100 + 0.5 * np.arange(200), index=idx))
    assert df["hist"].dropna().abs().max() < 1e-12
    assert mt.crossovers(df).empty


def test_cruce_real_despues_de_un_tramo_plano_se_conserva():
    idx = pd.bdate_range("2025-01-01", periods=200)
    precios = np.r_[np.full(120, 57.3), 57.3 + 0.2 * np.arange(1, 81)]
    ev = mt.crossovers(mt.macd(pd.Series(precios, index=idx)))
    assert set(ev["evento"]) == {"cruce_alcista", "cero_arriba"}


def test_rsi_class_value():
    assert mt.rsi_from_rs(3.6) == pytest.approx(78.2608, abs=1e-4)


def test_rsi_simple_matches_manual_window():
    x = random_walk(40)
    r = mt.rsi(x, 14, "simple")
    d = x.diff().iloc[1:15]            # dias 2 a 15 -> 14 cambios
    g = d.clip(lower=0).sum() / 14
    l = (-d).clip(lower=0).sum() / 14
    assert r.iloc[14] == pytest.approx(100 - 100 / (1 + g / l))


def test_resample_weekly_takes_friday_or_last_day():
    idx = pd.bdate_range("2026-08-31", "2026-09-18")
    s = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    s = s.drop(pd.Timestamp("2026-09-11"))  # simula viernes feriado
    w = mt.resample_close(s, "W")
    assert list(w.index.strftime("%Y-%m-%d")) == ["2026-09-04", "2026-09-10", "2026-09-18"]


def test_describe_last_returns_text():
    txt = mt.describe_last(mt.macd(random_walk(120)))
    assert "MACD =" in txt and "histograma" in txt


# --- fecha de corte y periodos incompletos ---------------------------------

def _serie_diaria(hasta="2026-09-21"):
    idx = pd.bdate_range("2024-09-02", hasta)
    return pd.Series(np.linspace(50.0, 60.0, len(idx)), index=idx)


def test_semana_en_curso_se_descarta(monkeypatch):
    """Corriendo en lunes, la última semana debe ser la del viernes anterior."""
    monkeypatch.setattr(pd.Timestamp, "today",
                        classmethod(lambda cls: pd.Timestamp("2026-09-21")))
    w = mt.resample_close(_serie_diaria(), "W")
    assert w.index[-1] == pd.Timestamp("2026-09-18")


def test_asof_fija_el_ultimo_viernes():
    """--asof hace el resultado independiente del día en que se corra."""
    w = mt.resample_close(_serie_diaria(), "W", asof="2026-09-18")
    assert w.index[-1] == pd.Timestamp("2026-09-18")
    # nada posterior al corte sobrevive
    assert (w.index <= pd.Timestamp("2026-09-18")).all()


def test_keep_partial_conserva_la_semana_en_curso(monkeypatch):
    monkeypatch.setattr(pd.Timestamp, "today",
                        classmethod(lambda cls: pd.Timestamp("2026-09-21")))
    w = mt.resample_close(_serie_diaria(), "W", keep_partial=True)
    assert w.index[-1] == pd.Timestamp("2026-09-21")


def test_mes_en_curso_se_descarta(monkeypatch):
    monkeypatch.setattr(pd.Timestamp, "today",
                        classmethod(lambda cls: pd.Timestamp("2026-09-21")))
    m = mt.resample_close(_serie_diaria(), "M")
    assert m.index[-1] == pd.Timestamp("2026-08-31")


def test_viernes_feriado_no_borra_la_semana():
    """Si el viernes fue feriado, la semana sigue contando con la fecha del jueves."""
    s = _serie_diaria("2026-09-18").drop(pd.Timestamp("2026-09-18"))
    w = mt.resample_close(s, "W", asof="2026-09-19")
    assert w.index[-1] == pd.Timestamp("2026-09-17")


def test_26_periodos_al_corte_son_los_esperados():
    """Los últimos 26 viernes hasta el 18-sep-2026 hacia atrás."""
    w = mt.resample_close(_serie_diaria(), "W", asof="2026-09-18")
    ult = w.tail(26)
    assert len(ult) == 26
    assert ult.index[-1] == pd.Timestamp("2026-09-18")
    assert ult.index[0] == pd.Timestamp("2026-03-27")


# --- resumen tecnico del panel ---------------------------------------------

def _df_con_indicadores(x):
    d = mt.macd(x)
    d["ppo"] = mt.ppo(x)
    d["rsi14"] = mt.rsi(x, 14, "wilder")
    return d


# Nota: se usan curvas cuadraticas y no rectas. En una recta perfecta el MACD
# converge a la constante 7*pendiente y el histograma queda en cero numerico
# (~1e-14), donde el signo es ruido de punto flotante y no significa nada.

def _acelerando(subiendo: bool, n: int = 200) -> pd.Series:
    t = np.arange(n)
    base = 100 + 0.004 * t ** 2 if subiendo else 300 - 0.004 * t ** 2
    return pd.Series(base, index=pd.bdate_range("2025-01-01", periods=n))


def test_resumen_sesgo_alcista_tres_de_tres():
    """Alza acelerada: MACD sobre cero, sobre su señal e histograma subiendo."""
    r = mt.resumen_tecnico(_df_con_indicadores(_acelerando(True)), "día")
    assert r["puntos"] == 3 and r["sesgo"] == "ALCISTA"
    assert r["lado_senal"] == "por encima" and r["lado_cero"] == "por encima"


def test_resumen_sesgo_bajista_cero_de_tres():
    r = mt.resumen_tecnico(_df_con_indicadores(_acelerando(False)), "día")
    assert r["puntos"] == 0 and r["sesgo"] == "BAJISTA"
    assert r["lado_senal"] == "por debajo" and r["lado_cero"] == "por debajo"


def test_resumen_momentum_nombra_el_signo():
    """Un histograma negativo que se hace más negativo NO debe decir 'creciendo'."""
    r = mt.resumen_tecnico(_df_con_indicadores(_acelerando(False)), "día")
    assert "bajista" in r["momentum"]
    assert "creciendo" not in r["momentum"]


def test_resumen_valores_coinciden_con_el_dataframe():
    x = random_walk(220)
    d = _df_con_indicadores(x)
    r = mt.resumen_tecnico(d, "día")
    v = d.dropna(subset=["hist"])
    assert r["macd"] == pytest.approx(v["macd"].iloc[-1])
    assert r["hist_prev"] == pytest.approx(v["hist"].iloc[-2])
    assert r["precio"] == pytest.approx(v["close"].iloc[-1])


def test_resumen_vacio_si_no_alcanza_la_historia():
    assert mt.resumen_tecnico(mt.macd(random_walk(30))) == {}


# ---------------------------------------------------------------------------
# Umbral de ruido: cada cuantos periodos cruza el MACD cuando no hay nada que
# encontrar. Ver scripts/random_walk_crossings.py.

def test_umbral_de_ruido_sale_de_una_caminata_aleatoria():
    """En 40 caminatas aleatorias el MACD(12,26,9) cruza en promedio cada ~12 periodos.

    El umbral del panel tiene que quedar en ese promedio. El anterior, 10, no
    habria marcado ninguna de las 40 caminatas, aunque todas son puro ruido.
    """
    from scripts import random_walk_crossings as rw

    f = rw.random_walk_baseline()
    assert abs(f.mean() - mt.PERIODOS_RUIDO) < 1.0
    assert f.min() > 10


def _senoide(periodo: int, n: int = 480) -> pd.Series:
    t = np.arange(n)
    return pd.Series(100 + 5 * np.sin(2 * np.pi * t / periodo),
                     index=pd.bdate_range("2024-01-01", periods=n))


@pytest.mark.parametrize("periodo, ruidosa", [(16, True), (40, False)])
def test_resumen_marca_lo_que_cruza_mas_seguido_que_el_umbral(periodo, ruidosa):
    """Un seno de periodo P cruza su señal dos veces por ciclo, o sea cada P/2."""
    r = mt.resumen_tecnico(mt.macd(_senoide(periodo)), "día")
    assert r["periodos_por_cruce"] == pytest.approx(periodo / 2, rel=0.05)
    assert r["ruidosa"] == ruidosa
    assert r["ruido_txt"] == f"1 cada {r['periodos_por_cruce']:.1f} días"
