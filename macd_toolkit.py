"""
macd_toolkit.py
===============

Medias moviles (SMA / EMA), MACD (12, 26, 9) y RSI implementados desde cero,
con verificacion contra pandas y utilidades para descargar datos, remuestrear
a cierres semanales (viernes) o mensuales, detectar cruces y graficar.

Convenciones (documentadas porque cada plataforma usa algo ligeramente distinto):

* ``P_t``      precio de cierre del periodo t (dia, semana o mes).
* SMA_N        promedio aritmetico de los ultimos N cierres. Existe desde t = N.
* EMA_N        EMA_t = alpha * P_t + (1 - alpha) * EMA_{t-1},  alpha = 2 / (N + 1).
               Semilla por defecto: EMA_N en t = N es la SMA de los primeros N
               cierres (convencion de libros de texto / StockCharts).
               Con ``seed="first"`` la semilla es EMA_1 = P_1, que es lo que hace
               ``Series.ewm(span=N, adjust=False)`` de pandas.
* MACD         EMA_12 - EMA_26                      (existe desde t = 26)
* Senal        EMA_9 aplicada a la serie MACD         (existe desde t = 26 + 9 - 1 = 34)
* Histograma   MACD - Senal                         (existe desde t = 34)

Uso rapido desde la terminal::

    python macd_toolkit.py FEMSAUBD.MX --freq W --years 2 --show 26
    python macd_toolkit.py NVDA --freq D --years 1 --show 26 --kind sma
    python macd_toolkit.py --csv mis_cierres.csv --freq W

Requisitos: numpy, pandas, matplotlib. ``yfinance`` solo si descargas datos.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

Kind = Literal["ema", "sma"]
Seed = Literal["sma", "first"]


# ---------------------------------------------------------------------------
# 1. Medias moviles
# ---------------------------------------------------------------------------

def alpha_from_span(n: int) -> float:
    """Factor de suavizamiento de una EMA de N periodos: alpha = 2 / (N + 1)."""
    if n < 1:
        raise ValueError("N debe ser >= 1")
    return 2.0 / (n + 1.0)


def sma(x: pd.Series, n: int) -> pd.Series:
    """Media movil simple de N periodos. Los primeros N-1 valores son NaN."""
    if n < 1:
        raise ValueError("N debe ser >= 1")
    return x.rolling(window=n, min_periods=n).mean()


def ema(x: pd.Series, n: int, seed: Seed = "sma", alpha: float | None = None) -> pd.Series:
    """
    Media movil exponencial calculada con la recursion explicita.

    Parametros
    ----------
    x     : serie de precios (se ignoran los NaN iniciales; no debe haber NaN intermedios).
    n     : periodo N. Define alpha = 2/(N+1) salvo que se pase ``alpha``.
    seed  : "sma"   -> el primer valor (posicion N-1 de los datos validos) es SMA_N.
            "first" -> el primer valor es el primer precio (igual que pandas adjust=False).
    alpha : factor de suavizamiento explicito (por ejemplo 1/N para el suavizado de Wilder).

    Devuelve una Serie con el mismo indice que ``x``.
    """
    a = alpha_from_span(n) if alpha is None else float(alpha)
    if not 0.0 < a <= 1.0:
        raise ValueError("alpha debe estar en (0, 1]")

    out = pd.Series(np.nan, index=x.index, dtype="float64")
    valid = x.dropna()
    if valid.index.size and x.loc[valid.index[0]:].isna().any():
        raise ValueError("La serie tiene huecos (NaN) despues del primer dato valido.")
    values = valid.to_numpy(dtype="float64")

    if seed == "sma":
        if values.size < n:
            return out
        start = n - 1
        prev = values[:n].mean()
    elif seed == "first":
        if values.size == 0:
            return out
        start = 0
        prev = values[0]
    else:
        raise ValueError("seed debe ser 'sma' o 'first'")

    res = np.full(values.size, np.nan)
    res[start] = prev
    for i in range(start + 1, values.size):
        prev = a * values[i] + (1.0 - a) * prev
        res[i] = prev

    out.loc[valid.index] = res
    return out


def moving_average(x: pd.Series, n: int, kind: Kind = "ema", seed: Seed = "sma") -> pd.Series:
    """Despachador comodo: SMA o EMA."""
    if kind == "sma":
        return sma(x, n)
    if kind == "ema":
        return ema(x, n, seed=seed)
    raise ValueError("kind debe ser 'ema' o 'sma'")


# ---------------------------------------------------------------------------
# 2. MACD
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MACDConfig:
    fast: int = 12
    slow: int = 26
    signal: int = 9
    kind: Kind = "ema"
    seed: Seed = "sma"

    def __post_init__(self) -> None:
        if not self.fast < self.slow:
            raise ValueError("fast debe ser menor que slow")

    @property
    def first_macd(self) -> int:
        """Numero de cierres necesarios para el primer valor de MACD."""
        return self.slow if (self.kind == "sma" or self.seed == "sma") else 1

    @property
    def first_signal(self) -> int:
        """Numero de cierres necesarios para el primer valor de la linea de senal."""
        if self.kind == "sma" or self.seed == "sma":
            return self.slow + self.signal - 1
        return 1


def macd(close: pd.Series, cfg: MACDConfig = MACDConfig()) -> pd.DataFrame:
    """
    Calcula las tres piezas del MACD.

    Columnas devueltas:
        close, ma_fast, ma_slow, macd, signal, hist
    """
    close = close.astype("float64")
    ma_fast = moving_average(close, cfg.fast, cfg.kind, cfg.seed)
    ma_slow = moving_average(close, cfg.slow, cfg.kind, cfg.seed)
    line = ma_fast - ma_slow
    # La senal se calcula sobre la serie MACD a partir de su primer valor valido.
    signal = moving_average(line, cfg.signal, cfg.kind, cfg.seed)
    hist = line - signal
    return pd.DataFrame(
        {
            "close": close,
            "ma_fast": ma_fast,
            "ma_slow": ma_slow,
            "macd": line,
            "signal": signal,
            "hist": hist,
        }
    )


def ppo(close: pd.Series, cfg: MACDConfig = MACDConfig()) -> pd.Series:
    """Percentage Price Oscillator: MACD normalizado, 100 * (MA_rapida - MA_lenta) / MA_lenta."""
    fast = moving_average(close, cfg.fast, cfg.kind, cfg.seed)
    slow = moving_average(close, cfg.slow, cfg.kind, cfg.seed)
    return 100.0 * (fast - slow) / slow


def crossovers(df: pd.DataFrame, tol: float | None = None) -> pd.DataFrame:
    """
    Detecta eventos en un DataFrame producido por ``macd``.

    * cruce_alcista : MACD pasa de estar por debajo (o igual) a por encima de la senal.
    * cruce_bajista : MACD pasa de estar por encima (o igual) a por debajo de la senal.
    * cero_arriba   : MACD cruza la linea cero hacia arriba (MA rapida supera a la lenta).
    * cero_abajo    : MACD cruza la linea cero hacia abajo.

    ``tol``: valores con ``|x| <= tol`` cuentan como cero. En tramos planos o
    perfectamente lineales el histograma es ruido de redondeo (~1e-15) que
    cambia de signo y produciria cruces falsos. Por defecto la tolerancia es
    1e-9 veces el mayor cierre (minimo 1e-9), muy por debajo de cualquier
    diferencia real entre MACD y senal.
    """
    if tol is None:
        escala = float(df["close"].abs().max()) if df["close"].notna().any() else 1.0
        tol = 1e-9 * max(1.0, escala)
    h = df["hist"].mask(df["hist"].abs() <= tol, 0.0)
    m = df["macd"].mask(df["macd"].abs() <= tol, 0.0)
    h_prev, m_prev = h.shift(1), m.shift(1)
    ok_h = h.notna() & h_prev.notna()
    ok_m = m.notna() & m_prev.notna()

    events = []
    for mask, label in (
        (ok_h & (h_prev <= 0) & (h > 0), "cruce_alcista"),
        (ok_h & (h_prev >= 0) & (h < 0), "cruce_bajista"),
        (ok_m & (m_prev <= 0) & (m > 0), "cero_arriba"),
        (ok_m & (m_prev >= 0) & (m < 0), "cero_abajo"),
    ):
        for ts in df.index[mask]:
            events.append({"fecha": ts, "evento": label,
                           "close": df.at[ts, "close"],
                           "macd": df.at[ts, "macd"],
                           "signal": df.at[ts, "signal"]})
    out = pd.DataFrame(events, columns=["fecha", "evento", "close", "macd", "signal"])
    return out.sort_values("fecha").reset_index(drop=True)


def describe_last(df: pd.DataFrame) -> str:
    """Lectura en texto del ultimo periodo (para el reporte)."""
    valid = df.dropna(subset=["hist"])
    if len(valid) < 2:
        return "Todavía no hay suficientes periodos para leer el histograma."
    # Se usa last["hist"] y no last.hist: .hist es un metodo de pandas.Series
    last, prev = valid.iloc[-1], valid.iloc[-2]
    m, s, h, h0 = last["macd"], last["signal"], last["hist"], prev["hist"]
    lado = "por encima" if m > 0 else "por debajo"
    rel = "arriba" if h > 0 else "abajo"
    tend = "creciendo" if abs(h) > abs(h0) else "reduciéndose"
    return (
        f"MACD = {m:.4f} ({lado} de cero), señal = {s:.4f}. "
        f"El MACD está {rel} de su señal y la distancia (histograma = {h:.4f}) "
        f"está {tend} respecto al periodo anterior ({h0:.4f})."
    )


# ---------------------------------------------------------------------------
# 3. RSI (lo visto en clase + version de Wilder)
# ---------------------------------------------------------------------------

def rsi_from_rs(rs: float) -> float:
    """RSI = 100 - 100 / (1 + RS)."""
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(close: pd.Series, n: int = 14, method: Literal["simple", "wilder"] = "wilder") -> pd.Series:
    """
    RSI de N periodos.

    method="simple": promedio aritmetico de ganancias y perdidas en una ventana
                     movil de N cambios (lo que se hizo en clase con los dias 2 a 15).
    method="wilder": primer promedio aritmetico y despues
                     prom_t = ((N-1) * prom_{t-1} + valor_t) / N, que es una EMA con alpha = 1/N.
    """
    delta = close.astype("float64").diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    if method == "simple":
        avg_g = gain.rolling(n, min_periods=n).mean()
        avg_l = loss.rolling(n, min_periods=n).mean()
    elif method == "wilder":
        avg_g = ema(gain, n, seed="sma", alpha=1.0 / n)
        avg_l = ema(loss, n, seed="sma", alpha=1.0 / n)
    else:
        raise ValueError("method debe ser 'simple' o 'wilder'")
    total = avg_g + avg_l
    out = 100.0 * avg_g / total
    return out.where(total != 0, 50.0)


# ---------------------------------------------------------------------------
# 4. Datos
# ---------------------------------------------------------------------------

def resample_close(close: pd.Series, freq: Literal["D", "W", "M"],
                   asof: str | pd.Timestamp | None = None,
                   keep_partial: bool = False) -> pd.Series:
    """
    D: sin cambios.
    W: ultimo cierre de cada semana que termina en viernes (si el viernes fue
       feriado se toma el ultimo dia habil de esa semana).
    M: ultimo cierre de cada mes.

    Parametros
    ----------
    asof : fecha de corte. Se descarta todo cierre posterior. Sirve para fijar
           el analisis a un viernes concreto y que el resultado no cambie segun
           el dia en que se corra el script.
    keep_partial : por defecto False, que elimina el ultimo periodo cuando
           todavia no termina. Si hoy es lunes, la semana en curso solo tiene un
           dia y su "cierre semanal" no es el cierre de un viernes; incluirla
           mete un punto falso al final de la serie y mueve el MACD. Con
           keep_partial=True se conserva, marcado con la fecha real del ultimo
           dia disponible.

    El indice devuelto es la fecha real del ultimo dia operado de cada periodo,
    no la etiqueta del contenedor. Asi, si el viernes fue feriado, se ve el
    jueves, que es el cierre que de verdad ocurrio.
    """
    close = close.dropna().sort_index()
    if asof is not None:
        asof = pd.Timestamp(asof).normalize()
        close = close.loc[:asof]
    if freq == "D":
        return close
    if close.empty:
        return close
    if freq == "W":
        rule = "W-FRI"
    elif freq == "M":
        rule = "ME" if int(pd.__version__.split(".")[0]) >= 2 else "M"
    else:
        raise ValueError("freq debe ser D, W o M")

    values = close.resample(rule).last()
    # Fecha real del ultimo dia operado dentro de cada contenedor.
    last_date = close.index.to_series().resample(rule).last()
    ok = values.notna()
    values, last_date = values[ok], last_date[ok]

    if not keep_partial and len(values):
        # values.index guarda la etiqueta del contenedor: el viernes de esa
        # semana o el ultimo dia de ese mes. El periodo esta cerrado solo si esa
        # fecha ya paso. Se compara contra asof, o contra hoy si no se dio asof.
        corte = asof if asof is not None else pd.Timestamp.today().normalize()
        if values.index[-1] > corte:
            values, last_date = values.iloc[:-1], last_date.iloc[:-1]

    out = pd.Series(values.to_numpy(), index=pd.DatetimeIndex(last_date.to_numpy()), name=close.name)
    return out.dropna()


def download_close(ticker: str, years: float = 2.0, column: str = "Close",
                   asof: str | pd.Timestamp | None = None) -> pd.Series:
    """
    Descarga cierres diarios con yfinance. Se fija auto_adjust=False para que
    "Close" sea el precio de cierre tal cual (el que ves en las paginas) y
    "Adj Close" el ajustado por dividendos y splits.

    ``asof`` fija el ultimo dia que se descarga y desde el cual se cuentan los
    anios de historia hacia atras.
    """
    frame = download_many([ticker], years=years, column=column, asof=asof)
    if ticker not in frame:
        raise SystemExit(f"Sin datos para {ticker}. Revisa el ticker (BMV usa sufijo .MX).")
    return frame[ticker]


def download_many(tickers: list[str], years: float = 2.0, column: str = "Close",
                  asof: str | pd.Timestamp | None = None) -> dict[str, pd.Series]:
    """
    Descarga varios tickers en una sola llamada a yfinance y devuelve un
    diccionario ticker -> serie de cierres diarios. Los tickers sin datos
    simplemente no aparecen en el resultado, en vez de abortar el proceso.
    """
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Instala yfinance:  pip install yfinance") from exc

    fin = pd.Timestamp.today().normalize() if asof is None else pd.Timestamp(asof).normalize()
    end = fin + pd.Timedelta(days=1)
    start = end - pd.Timedelta(days=int(365.25 * years) + 10)
    data = yf.download(list(tickers), start=start, end=end, interval="1d",
                       auto_adjust=False, progress=False, group_by="column")
    if data is None or data.empty:
        return {}

    out: dict[str, pd.Series] = {}
    for t in tickers:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                col = data[(column, t)]
            else:  # un solo ticker: yfinance devuelve columnas planas
                col = data[column]
        except KeyError:
            continue
        if isinstance(col, pd.DataFrame):
            col = col.iloc[:, 0]
        col = col.dropna()
        if col.empty:
            continue
        col.index = pd.DatetimeIndex(col.index).tz_localize(None)
        out[t] = col.rename(t)
    return out


def load_csv(path: str | Path, date_col: str | None = None, close_col: str | None = None) -> pd.Series:
    """Lee un CSV con columnas de fecha y cierre (detecta nombres comunes en espanol o ingles)."""
    df = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in df.columns}
    date_col = date_col or next(cols[c] for c in cols if c in ("date", "fecha", "datetime"))
    close_col = close_col or next(cols[c] for c in cols if c in ("close", "cierre", "ultimo", "último", "price", "precio"))
    s = pd.Series(pd.to_numeric(df[close_col], errors="coerce").to_numpy(),
                  index=pd.to_datetime(df[date_col], dayfirst=False), name=close_col)
    return s.dropna().sort_index()


# ---------------------------------------------------------------------------
# 5. Graficas
# ---------------------------------------------------------------------------

def resumen_tecnico(df: pd.DataFrame, periodo: str = "periodo") -> dict:
    """
    Condensa el estado del ultimo periodo en un diccionario listo para imprimir.

    Recibe el DataFrame COMPLETO (no el recortado para graficar), porque el
    ultimo cruce y la frecuencia de cruces se cuentan sobre toda la historia.

    El "sesgo" suma tres condiciones tecnicas independientes:

        1. MACD por encima de su senal   (histograma positivo)
        2. MACD por encima de cero       (la media rapida va arriba de la lenta)
        3. Histograma subiendo respecto al periodo anterior

    Tres de tres es una configuracion alcista de libro, cero de tres es bajista,
    y lo de en medio es lo que de verdad pasa casi siempre. El RSI se reporta
    aparte porque mide otra cosa y suele contradecir al MACD justo en los
    extremos, que es cuando mas importa verlo.

    Esto describe el estado de dos indicadores de precio. No sabe nada de la
    empresa, de su valuacion ni de las noticias, asi que no decide nada.
    """
    valido = df.dropna(subset=["hist"])
    if len(valido) < 2:
        return {}

    ult, prev = valido.iloc[-1], valido.iloc[-2]
    h, h0 = float(ult["hist"]), float(prev["hist"])
    m = float(ult["macd"])

    eventos = crossovers(df)
    cruces = eventos[eventos["evento"].isin(["cruce_alcista", "cruce_bajista"])]
    if len(cruces):
        ultimo = cruces.iloc[-1]
        f_cruce = pd.Timestamp(ultimo["fecha"])
        t_cruce = ultimo["evento"].replace("cruce_", "")
        desde = int((df.index > f_cruce).sum())
        cruce_txt = f"{t_cruce}, hace {desde}"
    else:
        t_cruce, cruce_txt = "sin cruce", "sin cruces en la historia"

    # Cada cuantos periodos ha cruzado historicamente: entre mas seguido cruza,
    # menos confiable es cualquier cruce individual en esa emisora.
    n_validos = int(df["hist"].notna().sum())
    if len(cruces) >= 2:
        frec = n_validos / len(cruces)
        ruido_txt = f"1 cada {frec:.0f} {periodo}s"
        # Se marca en rojo cuando cruza mas seguido que la propia linea de
        # senal (9 periodos): si el MACD se voltea antes de que su EMA de 9
        # alcance a acomodarse, ningun cruce individual llega a significar algo.
        ruidosa = frec < 10
    else:
        frec, ruido_txt, ruidosa = float("nan"), "sin historial", False

    condiciones = [h > 0, m > 0, h > h0]
    puntos = sum(condiciones)
    sesgo = {3: "ALCISTA", 2: "MIXTO, SESGO ALCISTA",
             1: "MIXTO, SESGO BAJISTA", 0: "BAJISTA"}[puntos]

    # "Creciendo" a secas confunde: un histograma negativo que se hace mas
    # negativo tambien "crece". Aqui se nombra la direccion con su signo.
    if h > 0:
        momentum = ("impulso alcista fortaleciéndose" if h > h0
                    else "impulso alcista perdiendo fuerza")
    else:
        momentum = ("impulso bajista perdiendo fuerza" if h > h0
                    else "impulso bajista fortaleciéndose")

    rsi = float(ult["rsi14"]) if "rsi14" in df.columns and pd.notna(ult.get("rsi14")) else None
    if rsi is None:
        rsi_txt = "n/d"
    elif rsi > 70:
        rsi_txt = f"{rsi:.1f}  sobrecompra"
    elif rsi < 30:
        rsi_txt = f"{rsi:.1f}  sobreventa"
    else:
        rsi_txt = f"{rsi:.1f}  equilibrio"

    p, p0 = float(ult["close"]), float(prev["close"])
    return {
        "fecha": pd.Timestamp(valido.index[-1]).date(),
        "precio": p, "precio_prev": p0,
        "var_pct": (p / p0 - 1.0) * 100.0 if p0 else float("nan"),
        "macd": m, "macd_prev": float(prev["macd"]),
        "senal": float(ult["signal"]), "senal_prev": float(prev["signal"]),
        "hist": h, "hist_prev": h0,
        "lado_senal": "por encima" if h > 0 else "por debajo",
        "lado_cero": "por encima" if m > 0 else "por debajo",
        "momentum": momentum,
        "periodo": periodo,
        "cruce_tipo": t_cruce, "cruce_txt": cruce_txt,
        "cruce_periodos": desde if len(cruces) else None,
        "ruido_txt": ruido_txt, "ruidosa": ruidosa, "cruces_total": len(cruces),
        "rsi": rsi, "rsi_txt": rsi_txt,
        "ppo": float(ult["ppo"]) if "ppo" in df.columns and pd.notna(ult.get("ppo")) else None,
        "puntos": puntos, "sesgo": sesgo,
    }


def plot_macd(df: pd.DataFrame, title: str, path: str | Path | None = None,
              labels: tuple[str, str] = ("EMA 12", "EMA 26"),
              resumen: dict | None = None):
    """
    Figura de dos paneles: precio con medias y MACD con senal e histograma.

    Si se pasa ``resumen`` (el diccionario de ``resumen_tecnico``), se agrega un
    tercer panel de texto abajo con el estado del ultimo periodo. Va en su propio
    eje, no encima de las graficas, asi que no tapa nada.
    """
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    if resumen:
        fig, (ax1, ax2, ax3) = plt.subplots(
            3, 1, figsize=(12, 8.6),
            gridspec_kw={"height_ratios": [3.0, 2.0, 1.15], "hspace": 0.16})
        ax1.sharex(ax2)
        # ax3 es texto, no comparte el eje x. Las fechas se ponen en ax2.
        for etq in ax1.get_xticklabels():
            etq.set_visible(False)
    else:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                       gridspec_kw={"height_ratios": [3, 2]})
        ax3 = None
    ax1.plot(df.index, df["close"], color="0.25", lw=1.3, label="Cierre")
    ax1.plot(df.index, df["ma_fast"], color="#1f5fa8", lw=1.4, label=labels[0])
    ax1.plot(df.index, df["ma_slow"], color="#b3362b", lw=1.4, label=labels[1])
    ax1.set_ylabel("Precio")
    ax1.set_title(title)
    ax1.legend(loc="best", frameon=False)
    ax1.grid(alpha=0.3)

    colors = np.where(df["hist"] >= 0, "#2e7d4f", "#b3362b")
    if len(df.index) > 1:
        step = pd.Series(df.index).diff().median()
        width = step * 0.7
    else:
        width = 1
    ax2.bar(df.index, df["hist"], width=width, color=colors, alpha=0.55, label="Histograma")
    ax2.plot(df.index, df["macd"], color="#1f5fa8", lw=1.4, label="MACD")
    ax2.plot(df.index, df["signal"], color="#d08a00", lw=1.4, label="Señal (9)")
    ax2.axhline(0, color="0.4", lw=0.8)
    ax2.set_ylabel("MACD")
    ax2.legend(loc="best", frameon=False, ncol=3)
    ax2.grid(alpha=0.3)

    if ax3 is not None:
        r = resumen
        ax3.axis("off")
        verde, rojo, gris = "#2e7d4f", "#b3362b", "#545e6b"
        col_sesgo = {"ALCISTA": verde, "MIXTO, SESGO ALCISTA": "#6b8f3f",
                     "MIXTO, SESGO BAJISTA": "#b87400", "BAJISTA": rojo}[r["sesgo"]]

        def celda(x, y, etiqueta, valor, color="#1b2430"):
            ax3.text(x, y, etiqueta, transform=ax3.transAxes,
                     fontsize=8.2 if len(etiqueta) <= 26 else 7.4,
                     color=gris, va="baseline")
            # La columna da para unos 22 caracteres a 10.5 pt; mas alla se
            # encoge para no invadir la celda vecina.
            n = len(valor)
            fs = 10.5 if n <= 22 else (9.4 if n <= 26 else 8.5)
            ax3.text(x, y - 0.20, valor, transform=ax3.transAxes, fontsize=fs,
                     color=color, va="baseline", fontweight="bold")

        ax3.add_patch(FancyBboxPatch(
            (0.0, 0.02), 1.0, 0.96, transform=ax3.transAxes,
            boxstyle="round,pad=0.006", linewidth=0.9,
            edgecolor="#c9cec4", facecolor="#fbfbf8", zorder=0, clip_on=False))

        xs = [0.020, 0.190, 0.360, 0.530]
        sgn = "+" if r["var_pct"] >= 0 else ""
        celda(xs[0], 0.80, f"Cierre  {r['fecha']}",
              f"{r['precio']:,.2f}  ({sgn}{r['var_pct']:.2f}%)",
              verde if r["var_pct"] >= 0 else rojo)
        celda(xs[1], 0.80, "MACD  (anterior)",
              f"{r['macd']:.4f}  ({r['macd_prev']:.4f})",
              verde if r["macd"] > 0 else rojo)
        celda(xs[2], 0.80, "Señal  (anterior)",
              f"{r['senal']:.4f}  ({r['senal_prev']:.4f})")
        celda(xs[3], 0.80, "Histograma  (anterior)",
              f"{r['hist']:+.4f}  ({r['hist_prev']:+.4f})",
              verde if r["hist"] > 0 else rojo)

        celda(xs[0], 0.38, "Cierre anterior", f"{r['precio_prev']:,.2f}")
        celda(xs[1], 0.38, "RSI (14)", r["rsi_txt"],
              rojo if (r["rsi"] or 50) > 70 or (r["rsi"] or 50) < 30 else "#1b2430")
        unidad = {"día": "días", "semana": "semanas", "mes": "meses"}.get(
            r.get("periodo", ""), "periodos")
        celda(xs[2], 0.38, f"Último cruce ({unidad})", r["cruce_txt"],
              verde if r["cruce_tipo"] == "alcista" else rojo)
        celda(xs[3], 0.38, f"Cruces históricos ({r['cruces_total']})",
              r["ruido_txt"], rojo if r["ruidosa"] else "#1b2430")

        # Bloque de lectura, a la derecha, separado de la reja de numeros.
        ax3.add_patch(FancyBboxPatch(
            (0.706, 0.12), 0.278, 0.80, transform=ax3.transAxes,
            boxstyle="round,pad=0.005", linewidth=1.4,
            edgecolor=col_sesgo, facecolor="#ffffff", zorder=1, clip_on=False))
        ax3.text(0.845, 0.80, "LECTURA TÉCNICA", transform=ax3.transAxes,
                 fontsize=8, color=gris, ha="center", zorder=2)
        ax3.text(0.845, 0.55, r["sesgo"], transform=ax3.transAxes, fontsize=12,
                 color=col_sesgo, ha="center", fontweight="bold", zorder=2)
        ax3.text(0.845, 0.36, f"{r['puntos']} de 3 condiciones alcistas",
                 transform=ax3.transAxes, fontsize=8, color=gris, ha="center", zorder=2)
        ax3.text(0.845, 0.20, r["momentum"], transform=ax3.transAxes, fontsize=8,
                 color="#1b2430", ha="center", zorder=2, style="italic")

        ppo_txt = f"   ·   PPO {r['ppo']:+.2f}%" if r["ppo"] is not None else ""
        ax3.text(0.020, 0.06,
                 f"MACD {r['lado_senal']} de su señal y {r['lado_cero']} de cero{ppo_txt}"
                 "   ·   Lectura de indicadores de precio, no es recomendación de inversión.",
                 transform=ax3.transAxes, fontsize=7.5, color=gris, va="baseline")

    if ax3 is not None:
        # tight_layout no sabe medir el panel de texto y lo aplasta, asi que
        # con resumen se fijan los margenes a mano.
        for etq in ax2.get_xticklabels():
            etq.set_rotation(22)
            etq.set_horizontalalignment("right")
        fig.subplots_adjust(left=0.088, right=0.985, top=0.945, bottom=0.035)
        # Se baja el panel de texto hasta el margen inferior para dejarle aire a
        # las fechas rotadas de ax2, que si no se le encaraman encima.
        p3 = ax3.get_position()
        ax3.set_position([p3.x0, 0.030, p3.width, p3.height * 0.90])
    else:
        fig.autofmt_xdate()
        fig.tight_layout()
    if path is not None:
        fig.savefig(path, dpi=160)
        plt.close(fig)
    return fig


# ---------------------------------------------------------------------------
# 6. Linea de comandos
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    if args.csv:
        daily = load_csv(args.csv)
        name = Path(args.csv).stem
    elif args.ticker:
        daily = download_close(args.ticker, years=args.years, column=args.column, asof=args.asof)
        name = args.ticker
    else:
        raise SystemExit("Indica un ticker o --csv")

    close = resample_close(daily, args.freq, asof=args.asof, keep_partial=args.incluir_parcial)
    if close.empty:
        raise SystemExit(f"No quedaron periodos para {name} con los filtros dados.")
    cfg = MACDConfig(args.fast, args.slow, args.signal, kind=args.kind, seed=args.seed)
    if len(close) < cfg.first_signal:
        print(f"Aviso: hay {len(close)} periodos y la línea de señal necesita {cfg.first_signal}.")
    df = macd(close, cfg)
    df["rsi14"] = rsi(close, 14, "wilder")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{name.replace('.', '_')}_{args.freq}_{args.kind}"
    df.to_csv(out_dir / f"{tag}_completo.csv", float_format="%.6f")
    shown = df.tail(args.show)
    shown.to_csv(out_dir / f"{tag}_ultimos{args.show}.csv", float_format="%.6f")
    crossovers(df).to_csv(out_dir / f"{tag}_eventos.csv", index=False, float_format="%.6f")
    labels = (f"{args.kind.upper()} {args.fast}", f"{args.kind.upper()} {args.slow}")
    etiqueta_periodo = {"D": "día", "W": "semana", "M": "mes"}[args.freq]
    plot_macd(shown, f"{name}  MACD({args.fast},{args.slow},{args.signal})  frecuencia {args.freq}  ({args.kind.upper()})",
              out_dir / f"{tag}.png", labels=labels,
              resumen=resumen_tecnico(df, etiqueta_periodo))

    print(f"{len(close)} periodos cargados; se reportan los últimos {len(shown)}.")
    print(f"Último periodo: {close.index[-1].date()}  (primero: {close.index[0].date()})")
    print(describe_last(df))
    print(f"Archivos en {out_dir.resolve()}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MACD con SMA/EMA desde cero")
    p.add_argument("ticker", nargs="?", help="Ticker de Yahoo Finance, p. ej. FEMSAUBD.MX, NVDA")
    p.add_argument("--csv", help="CSV propio con columnas fecha/cierre (en vez de descargar)")
    p.add_argument("--freq", choices=["D", "W", "M"], default="W")
    p.add_argument("--years", type=float, default=2.0, help="Historia a descargar (años)")
    p.add_argument("--show", type=int, default=26, help="Periodos a graficar/reportar")
    p.add_argument("--fast", type=int, default=12)
    p.add_argument("--slow", type=int, default=26)
    p.add_argument("--signal", type=int, default=9)
    p.add_argument("--kind", choices=["ema", "sma"], default="ema")
    p.add_argument("--seed", choices=["sma", "first"], default="sma")
    p.add_argument("--column", choices=["Close", "Adj Close"], default="Close")
    p.add_argument("--asof", default=None,
                   help="Fecha de corte AAAA-MM-DD. El análisis termina ahí (p. ej. 2026-09-18)")
    p.add_argument("--incluir-parcial", dest="incluir_parcial", action="store_true",
                   help="Conserva el periodo en curso aunque todavía no cierre")
    p.add_argument("--out", default="resultados")
    return p


if __name__ == "__main__":
    run(build_parser().parse_args())
