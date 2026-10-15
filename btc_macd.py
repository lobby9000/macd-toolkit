"""
btc_macd.py
===========

Bitcoin con pesos: cuanto cuesta de verdad comprarlo en cada plataforma y que
habria pasado con tres maneras de hacerlo.

    python btc_macd.py cotiza
    python btc_macd.py cotiza --manual "Mercado Pago" 1561000 1498000
    python btc_macd.py cotiza --manual "Mercado Pago" 1.503.231,35 1.487.661,56 --momento "2026-10-14 22:59"
    python btc_macd.py analiza --semanal 500
    python btc_macd.py analiza --desde 2018-01-01 --vela 1d --out resultados_btc

``cotiza`` lee en vivo el libro de ordenes BTC/MXN de las plataformas con API
publica (Bitso y Binance) y mide contra el precio medio de Bitso los precios de
compra y venta que anotas de las apps sin API, como Mercado Pago. Con
``--momento`` anotas precios que viste antes, por ejemplo en una captura de
pantalla: se miden contra el ultimo precio operado en Bitso en ese minuto. Cada
cotizacion se guarda en cotizaciones.csv y ``analiza`` usa el promedio de lo
medido.

``analiza`` descarga velas BTC/MXN de Bitso, calcula PPO, RSI, ATR y caida
desde el maximo, y simula con los costos de cada plataforma tres estrategias
con el mismo dinero: comprar todo el primer dia y mantener, comprar una
cantidad fija cada semana, y entrar y salir con los cruces del PPO.

Convenciones:

* Los costos se miden contra el precio medio m = (compra + venta) / 2 del libro
  de referencia. Comprar A pesos da A (1 - c_compra) / (m (1 + a)) bitcoin y
  vender Q bitcoin da Q m (1 - b) (1 - c_venta) pesos, con ``a`` la prima del
  precio de compra sobre m y ``b`` el descuento del precio de venta.
* El PPO es el MACD dividido entre la EMA lenta, en porcentaje. Bitcoin ha
  cotizado en niveles muy distintos, y un MACD de 5,000 pesos no significa lo
  mismo con bitcoin a 100 mil pesos que a 1.5 millones.
* Un cruce que se ve al cierre de una vela se opera al cierre de la siguiente
  (``--retraso 1``), para no usar un precio que todavia no existia.
* El valor de cada estrategia es lo que recibirias si vendieras ese dia, ya
  con el spread y la comision de venta.

Describe costos y lo que habria pasado con un indicador. No es una
recomendacion de inversion.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

import macd_toolkit as mt

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

RAIZ = Path(__file__).resolve().parent
ZONA = "America/Mexico_City"

BITSO = "https://api.bitso.com/v3"
BINANCE = ("https://api.binance.com", "https://data-api.binance.vision")
COINMETRICS_CSV = "https://raw.githubusercontent.com/coinmetrics/data/master/csv/btc.csv"
TIPO_CAMBIO_CSV = "https://raw.githubusercontent.com/datasets/exchange-rates/main/data/daily.csv"

VELAS = {"1d": 86_400, "4h": 14_400, "1h": 3_600}
NOMBRE_VELA = {"1d": "velas diarias", "4h": "velas de 4 horas", "1h": "velas de 1 hora"}
ESTRATEGIAS = {
    "mantener": "Comprar todo y mantener",
    "semanal": "Compra fija cada semana",
    "cruces": "Cruces del PPO",
}
CALENTAMIENTO = 200  # velas extra antes de --desde para que EMA, RSI y ATR ya esten asentados


class ErrorDatos(Exception):
    """No se pudieron obtener o interpretar datos (red, API, archivo o configuracion)."""


# ---------------------------------------------------------------------------
# 1. Red
# ---------------------------------------------------------------------------

def _pedir(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "macd-toolkit/btc_macd (github.com/lobby9000/macd-toolkit)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ErrorDatos(f"No pude leer {url}: {exc}") from None


def obtener_json(url: str, timeout: float = 20.0):
    try:
        return json.loads(_pedir(url, timeout).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ErrorDatos(f"{url} no regresó JSON: {exc}") from None


def obtener_texto(url: str, timeout: float = 90.0) -> str:
    return _pedir(url, timeout).decode("utf-8")


# ---------------------------------------------------------------------------
# 2. Plataformas y costos
# ---------------------------------------------------------------------------

def clave(nombre: str) -> str:
    """Nombre sin acentos, mayusculas ni espacios repetidos, para comparar."""
    s = unicodedata.normalize("NFKD", str(nombre))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def numero(texto: str) -> float:
    """
    Un precio escrito como lo muestra una app: 1503231.35, 1,503,231.35, o como
    Mercado Pago, 1.503.231,35 o 1503231,35. Con punto y coma, el ultimo es el
    decimal. Con uno solo, si separa grupos de tres digitos son miles (25,000 o
    25.000) y si no es el decimal (25,5).
    """
    s = "".join(str(texto).replace("$", "").split()).replace("_", "")
    signo = ""
    if s.startswith("-"):
        signo, s = "-", s[1:]
    if not s or not s[0].isdigit() or any(c not in "0123456789.," for c in s):
        raise ErrorDatos(f"{texto!r} no es un número")
    decimal = ""
    if "." in s and "," in s:
        decimal = "." if s.rfind(".") > s.rfind(",") else ","
    elif "." in s or "," in s:
        signo_sep = "." if "." in s else ","
        trozos = s.split(signo_sep)
        if len(trozos) == 2 and len(trozos[1]) != 3:
            decimal = signo_sep
    entera, fraccion = s, ""
    if decimal:
        entera, fraccion = s[:s.rfind(decimal)], s[s.rfind(decimal) + 1:]
        if not fraccion.isdigit():
            raise ErrorDatos(f"{texto!r} no es un número")
    if "." in entera or "," in entera:
        miles = "." if "." in entera else ","
        grupos = entera.split(miles)
        if (any(not g.isdigit() for g in grupos) or not 1 <= len(grupos[0]) <= 3
                or any(len(g) != 3 for g in grupos[1:])):
            raise ErrorDatos(f"{texto!r} no es un número: los miles van en grupos de tres")
        entera = "".join(grupos)
    return float(signo + entera + ("." + fraccion if fraccion else ""))


@dataclass(frozen=True)
class Plataforma:
    nombre: str
    comision_compra: float        # fraccion: 0.015 = 1.5%
    comision_venta: float
    spread: float | str           # fraccion total, "libro" o "por_medir"
    api: str | None = None
    spread_tipico: float | None = None
    omitir_sin_medicion: bool = False
    fuente: str = ""
    nota: str = ""


_CLAVES = {"nombre", "comision_compra", "comision_venta", "spread", "api", "spread_tipico",
           "omitir_sin_medicion", "fuente", "nota"}


def _porcentaje(valor, campo: str, nombre: str) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ErrorDatos(f"{nombre}: {campo} debe ser un número en %, no {valor!r}")
    if not 0 <= valor < 100:
        raise ErrorDatos(f"{nombre}: {campo} = {valor} está fuera de 0 a 100 %")
    return float(valor) / 100.0


def cargar_plataformas(ruta: str | Path) -> tuple[list[Plataforma], dict]:
    """Lee plataformas.toml. Devuelve las plataformas en el orden del archivo y la seccion [referencia]."""
    ruta = Path(ruta)
    try:
        with open(ruta, "rb") as f:
            datos = tomllib.load(f)
    except FileNotFoundError:
        raise ErrorDatos(f"No encuentro {ruta}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ErrorDatos(f"{ruta} no es TOML válido: {exc}") from None

    referencia = dict(datos.get("referencia", {}))
    if referencia.get("api", "bitso") not in LIBROS:
        raise ErrorDatos(f"[referencia] api debe ser una de {sorted(LIBROS)}")
    plataformas: list[Plataforma] = []
    vistos: set[str] = set()
    for i, p in enumerate(datos.get("plataforma", []), 1):
        nombre = str(p.get("nombre", "")).strip()
        if not nombre:
            raise ErrorDatos(f"La plataforma {i} de {ruta.name} no tiene nombre")
        extra = set(p) - _CLAVES
        if extra:
            raise ErrorDatos(f"{nombre}: claves desconocidas {sorted(extra)}")
        if clave(nombre) in vistos:
            raise ErrorDatos(f"{nombre} aparece dos veces en {ruta.name}")
        vistos.add(clave(nombre))

        spread = p.get("spread", "por_medir")
        api = p.get("api")
        if isinstance(spread, str):
            if spread not in ("libro", "por_medir"):
                raise ErrorDatos(f'{nombre}: spread debe ser un número, "libro" o "por_medir"')
            if spread == "libro" and api not in LIBROS:
                raise ErrorDatos(f'{nombre}: spread = "libro" necesita api = {" o ".join(sorted(LIBROS))}')
        else:
            spread = _porcentaje(spread, "spread", nombre)
        if api is not None and api not in LIBROS:
            raise ErrorDatos(f"{nombre}: api {api!r} no existe; opciones: {sorted(LIBROS)}")
        tipico = p.get("spread_tipico")
        plataformas.append(Plataforma(
            nombre=nombre,
            comision_compra=_porcentaje(p.get("comision_compra", 0), "comision_compra", nombre),
            comision_venta=_porcentaje(p.get("comision_venta", 0), "comision_venta", nombre),
            spread=spread,
            api=api,
            spread_tipico=None if tipico is None else _porcentaje(tipico, "spread_tipico", nombre),
            omitir_sin_medicion=bool(p.get("omitir_sin_medicion", False)),
            fuente=str(p.get("fuente", "")),
            nota=str(p.get("nota", "")),
        ))
    if not plataformas:
        raise ErrorDatos(f"{ruta.name} no tiene ninguna [[plataforma]]")
    return plataformas, referencia


@dataclass(frozen=True)
class Costos:
    """Costos efectivos de una plataforma como fracciones del precio medio."""
    nombre: str
    comision_compra: float
    comision_venta: float
    prima_compra: float       # a: compras a m (1 + a)
    descuento_venta: float    # b: vendes a m (1 - b)
    origen_spread: str = ""
    spread_medido: bool = True

    def btc_por_pesos(self, pesos: float, medio: float) -> float:
        return pesos * (1.0 - self.comision_compra) / (medio * (1.0 + self.prima_compra))

    def pesos_por_btc(self, btc: float, medio: float) -> float:
        return btc * medio * (1.0 - self.descuento_venta) * (1.0 - self.comision_venta)

    @property
    def spread(self) -> float:
        """(compra - venta) / precio medio."""
        return self.prima_compra + self.descuento_venta

    @property
    def factor_ida_vuelta(self) -> float:
        """Fraccion que regresa si compras y vendes de inmediato."""
        return ((1.0 - self.comision_compra) * (1.0 - self.comision_venta)
                * (1.0 - self.descuento_venta) / (1.0 + self.prima_compra))

    @property
    def ida_vuelta(self) -> float:
        return 1.0 - self.factor_ida_vuelta

    def componentes(self) -> dict[str, float]:
        """Reparto exacto del costo de ida y vuelta, en el orden en que se paga: suma ida_vuelta."""
        r = (1.0 - self.descuento_venta) / (1.0 + self.prima_compra)
        cc, cv = self.comision_compra, self.comision_venta
        return {"comision_compra": cc,
                "spread": (1.0 - cc) * (1.0 - r),
                "comision_venta": (1.0 - cc) * r * cv}


SIN_COSTOS = Costos("Sin costos", 0.0, 0.0, 0.0, 0.0, "referencia sin costos")


# ---------------------------------------------------------------------------
# 3. Cotizaciones: libros en vivo y precios anotados a mano
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cotizacion:
    plataforma: str
    compra: float          # precio al que compras
    venta: float           # precio al que vendes
    referencia: float      # precio medio de referencia en ese momento
    momento: pd.Timestamp  # UTC
    origen: str            # "api" o "manual"

    def __post_init__(self):
        if not (self.venta > 0 and self.compra >= self.venta and self.referencia > 0):
            raise ErrorDatos(f"{self.plataforma}: la compra ({self.compra:,.2f}) debe ser mayor o igual "
                             f"que la venta ({self.venta:,.2f}) y ambas positivas")

    @property
    def prima_compra(self) -> float:
        return self.compra / self.referencia - 1.0

    @property
    def descuento_venta(self) -> float:
        return 1.0 - self.venta / self.referencia

    @property
    def spread(self) -> float:
        return (self.compra - self.venta) / self.referencia


Libro = tuple[float, float, pd.Timestamp]  # (venta = bid, compra = ask, momento UTC)


def libro_bitso(obtener: Callable = obtener_json) -> Libro:
    d = obtener(f"{BITSO}/ticker/?book=btc_mxn")
    if not isinstance(d, dict) or not d.get("success") or "payload" not in d:
        raise ErrorDatos(f"Bitso no regresó el libro: {str(d)[:200]}")
    p = d["payload"]
    try:
        bid, ask = float(p["bid"]), float(p["ask"])
    except (KeyError, TypeError, ValueError):
        raise ErrorDatos(f"Bitso: respuesta sin bid/ask: {str(p)[:200]}") from None
    momento = pd.Timestamp(p.get("created_at") or pd.Timestamp.now(tz="UTC"))
    momento = momento.tz_localize("UTC") if momento.tzinfo is None else momento.tz_convert("UTC")
    return _validar_libro("Bitso", bid, ask, momento)


def libro_binance(obtener: Callable = obtener_json) -> Libro:
    ultimo: Exception | None = None
    for base in BINANCE:
        try:
            d = obtener(f"{base}/api/v3/ticker/bookTicker?symbol=BTCMXN")
            return _validar_libro("Binance", float(d["bidPrice"]), float(d["askPrice"]),
                                  pd.Timestamp.now(tz="UTC"))
        except (ErrorDatos, KeyError, TypeError, ValueError) as exc:
            ultimo = exc
    raise ErrorDatos(f"No pude leer el libro BTC/MXN de Binance: {ultimo}")


def _validar_libro(nombre: str, bid: float, ask: float, momento: pd.Timestamp) -> Libro:
    if not (bid > 0 and ask >= bid):
        raise ErrorDatos(f"{nombre}: libro inválido (bid {bid}, ask {ask})")
    return bid, ask, momento


LIBROS: dict[str, Callable[..., Libro]] = {"bitso": libro_bitso, "binance": libro_binance}


def leer_libros(apis: set[str], obtener: Callable = obtener_json) -> tuple[dict[str, Libro], list[str]]:
    libros: dict[str, Libro] = {}
    avisos: list[str] = []
    for api in sorted(apis):
        try:
            libros[api] = LIBROS[api](obtener)
        except ErrorDatos as exc:
            avisos.append(str(exc))
    return libros, avisos


def medio(libro: Libro) -> float:
    return (libro[0] + libro[1]) / 2.0


def precio_bitso_en(momento: pd.Timestamp, obtener: Callable = obtener_json) -> float:
    """
    Ultimo precio operado en el libro BTC/MXN de Bitso al terminar el minuto de
    ``momento`` (UTC), de sus velas de un minuto. Es la referencia de una
    cotizacion anotada despues, como la de una captura de pantalla, porque el
    libro de ese momento ya no se puede leer. El spread de Bitso ronda 0.04%, asi
    que este precio y el precio medio difieren en centesimas de punto.
    """
    minuto = pd.Timestamp(momento).tz_convert("UTC").floor("min")
    ms = lambda t: int(t.timestamp() * 1000)  # noqa: E731
    inicio, fin = minuto - pd.Timedelta(minutes=30), minuto + pd.Timedelta(minutes=1)
    d = obtener(f"{BITSO}/ohlc?book=btc_mxn&time_bucket=60&start={ms(inicio)}&end={ms(fin)}")
    if not isinstance(d, dict) or d.get("success") is False or "payload" not in d:
        raise ErrorDatos(f"Bitso no regresó velas de un minuto: {str(d)[:200]}")
    try:
        velas = sorted((int(r["bucket_start_time"]), float(r["last_rate"])) for r in d["payload"])
    except (KeyError, TypeError, ValueError):
        raise ErrorDatos(f"Bitso: velas de un minuto sin precio: {str(d['payload'])[:200]}") from None
    velas = [v for v in velas if v[0] <= ms(minuto)]
    if not velas:
        raise ErrorDatos(f"Bitso no tiene operaciones en la media hora antes de {minuto:%Y-%m-%d %H:%M} UTC")
    return velas[-1][1]


_COLUMNAS_REGISTRO = ["momento_utc", "plataforma", "compra", "venta", "referencia", "origen"]


def guardar_cotizaciones(cotizaciones: list[Cotizacion], ruta: str | Path) -> None:
    ruta = Path(ruta)
    nuevo = not ruta.exists() or ruta.stat().st_size == 0
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(_COLUMNAS_REGISTRO)
        for c in cotizaciones:
            w.writerow([c.momento.strftime("%Y-%m-%dT%H:%M:%SZ"), c.plataforma, f"{c.compra:.2f}",
                        f"{c.venta:.2f}", f"{c.referencia:.2f}", c.origen])


def leer_cotizaciones(ruta: str | Path) -> list[Cotizacion]:
    ruta = Path(ruta)
    if not ruta.exists():
        return []
    salida: list[Cotizacion] = []
    with open(ruta, newline="", encoding="utf-8") as f:
        for n, fila in enumerate(csv.DictReader(f), 2):
            try:
                salida.append(Cotizacion(
                    plataforma=fila["plataforma"], compra=float(fila["compra"]), venta=float(fila["venta"]),
                    referencia=float(fila["referencia"]), momento=pd.Timestamp(fila["momento_utc"]),
                    origen=fila.get("origen", "manual")))
            except (KeyError, TypeError, ValueError, ErrorDatos) as exc:
                raise ErrorDatos(f"{ruta.name}, renglón {n}: {exc}") from None
    return salida


def costos_de(p: Plataforma, *, libros: dict[str, Libro], registro: list[Cotizacion]) -> Costos | None:
    """
    Costos efectivos de una plataforma. El spread sale, en este orden, de:
    el numero fijo del archivo; el libro en vivo; el promedio de las cotizaciones
    guardadas; el spread tipico del archivo. Una plataforma "por_medir" sin
    cotizaciones queda solo con sus comisiones y marcada, o fuera si
    ``omitir_sin_medicion``.
    """
    base = dict(nombre=p.nombre, comision_compra=p.comision_compra, comision_venta=p.comision_venta)
    if isinstance(p.spread, float):
        return Costos(**base, prima_compra=p.spread / 2, descuento_venta=p.spread / 2,
                      origen_spread=f"fijo en el archivo, {100 * p.spread:.2f}%")
    medidas = [c for c in registro if clave(c.plataforma) == clave(p.nombre)]
    if p.spread == "libro" and p.api in libros:
        bid, ask, momento = libros[p.api]
        m = (bid + ask) / 2
        return Costos(**base, prima_compra=ask / m - 1, descuento_venta=1 - bid / m,
                      origen_spread=f"libro en vivo, {momento:%Y-%m-%d %H:%M} UTC")
    if medidas:
        a = float(np.mean([c.prima_compra for c in medidas]))
        b = float(np.mean([c.descuento_venta for c in medidas]))
        if len(medidas) == 1:
            q = medidas[0]
            origen = f"{'libro guardado' if q.origen == 'api' else 'cotización en la app'}, {q.momento:%Y-%m-%d %H:%M} UTC"
        else:
            d1, d2 = min(c.momento for c in medidas), max(c.momento for c in medidas)
            rango = f"{d1:%Y-%m-%d}" if d1.date() == d2.date() else f"{d1:%Y-%m-%d} a {d2:%Y-%m-%d}"
            origen = f"promedio de {len(medidas)} cotizaciones, {rango}"
        return Costos(**base, prima_compra=a, descuento_venta=b, origen_spread=origen)
    if p.spread == "libro" and p.spread_tipico is not None:
        return Costos(**base, prima_compra=p.spread_tipico / 2, descuento_venta=p.spread_tipico / 2,
                      origen_spread=f"típico del archivo, {100 * p.spread_tipico:.2f}% (sin conexión)")
    if p.omitir_sin_medicion:
        return None
    return Costos(**base, prima_compra=0.0, descuento_venta=0.0,
                  origen_spread="sin medir: solo comisiones", spread_medido=False)


def resolver_costos(plataformas: list[Plataforma], libros: dict[str, Libro],
                    registro: list[Cotizacion]) -> tuple[list[Costos], list[str]]:
    costos, avisos = [], []
    for p in plataformas:
        c = costos_de(p, libros=libros, registro=registro)
        if c is None:
            avisos.append(f"{p.nombre} queda fuera: su spread no está medido "
                          f'(python btc_macd.py cotiza --manual "{p.nombre}" COMPRA VENTA).')
            continue
        if not c.spread_medido:
            avisos.append(f"{p.nombre}: spread sin medir, el costo solo incluye sus comisiones.")
        costos.append(c)
    return costos, avisos


# ---------------------------------------------------------------------------
# 4. Velas
# ---------------------------------------------------------------------------

def _ms_local(fecha: pd.Timestamp) -> int:
    return int(pd.Timestamp(fecha).tz_localize(ZONA).tz_convert("UTC").timestamp() * 1000)


def velas_bitso(desde: str | pd.Timestamp, hasta: str | pd.Timestamp, vela: str = "1d",
                obtener: Callable = obtener_json, ahora: pd.Timestamp | None = None,
                pausa: float = 0.2) -> pd.DataFrame:
    """
    Velas BTC/MXN de Bitso (endpoint publico /v3/ohlc). Las velas diarias de
    Bitso empiezan a la medianoche de la Ciudad de Mexico y se etiquetan con esa
    fecha. La vela que todavia no cierra se descarta.
    """
    segundos = VELAS[vela]
    inicio = _ms_local(pd.Timestamp(desde).normalize())
    fin = _ms_local(pd.Timestamp(hasta).normalize() + pd.Timedelta(days=1))
    paso = 300 * segundos * 1000
    filas: dict[int, dict] = {}
    t = inicio
    while t < fin:
        t2 = min(t + paso, fin)
        d = obtener(f"{BITSO}/ohlc?book=btc_mxn&time_bucket={segundos}&start={t}&end={t2}")
        if not isinstance(d, dict) or d.get("success") is False or "payload" not in d:
            raise ErrorDatos(f"Bitso no regresó velas: {str(d)[:200]}")
        for r in d["payload"]:
            filas[int(r["bucket_start_time"])] = r
        t = t2
        if t < fin and pausa:
            time.sleep(pausa)
    ahora_ms = (pd.Timestamp.now(tz="UTC") if ahora is None else pd.Timestamp(ahora)).timestamp() * 1000
    completas = [r for k, r in sorted(filas.items()) if k + segundos * 1000 <= ahora_ms]
    if not completas:
        raise ErrorDatos("Bitso no regresó velas completas para ese periodo")
    indice = (pd.to_datetime([int(r["bucket_start_time"]) for r in completas], unit="ms", utc=True)
              .tz_convert(ZONA).tz_localize(None))
    if vela == "1d":
        indice = indice.normalize()
    df = pd.DataFrame({
        "open": [float(r["first_rate"]) for r in completas],
        "high": [float(r["max_rate"]) for r in completas],
        "low": [float(r["min_rate"]) for r in completas],
        "close": [float(r["last_rate"]) for r in completas],
        "volume": [float(r.get("volume", 0) or 0) for r in completas],
    }, index=indice)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.loc[pd.Timestamp(desde):pd.Timestamp(hasta) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)]
    return completar(df, vela, fuente="Bitso, libro BTC/MXN")


def velas_coinmetrics(desde: str | pd.Timestamp, hasta: str | pd.Timestamp,
                      obtener: Callable = obtener_texto) -> pd.DataFrame:
    """
    Respaldo sin llave: precio de referencia diario de CoinMetrics (USD) por el
    tipo de cambio diario de la Reserva Federal (FRED DEXMXUS, via datasets en
    GitHub). Solo hay cierres, asi que el ATR usa el cambio entre cierres.
    """
    btc = pd.read_csv(io.StringIO(obtener(COINMETRICS_CSV)), usecols=["time", "PriceUSD"])
    btc = pd.Series(pd.to_numeric(btc["PriceUSD"], errors="coerce").to_numpy(),
                    index=pd.to_datetime(btc["time"])).dropna()
    fx = pd.read_csv(io.StringIO(obtener(TIPO_CAMBIO_CSV)))
    fx = fx[fx["Country"] == "Mexico"]
    fx = pd.Series(pd.to_numeric(fx["Exchange rate"], errors="coerce").to_numpy(),
                   index=pd.to_datetime(fx["Date"])).dropna()
    if btc.empty or fx.empty:
        raise ErrorDatos("CoinMetrics o el tipo de cambio vinieron vacíos")
    dias = pd.date_range(min(btc.index[0], fx.index[0]), btc.index[-1], freq="D")
    mxn = fx.reindex(dias).ffill().reindex(btc.index)
    close = (btc * mxn).dropna()
    close = close.loc[pd.Timestamp(desde):pd.Timestamp(hasta)]
    if close.empty:
        raise ErrorDatos("CoinMetrics no tiene datos en ese periodo")
    df = pd.DataFrame({"open": np.nan, "high": np.nan, "low": np.nan, "close": close, "volume": np.nan})
    return completar(df, "1d", fuente="CoinMetrics (USD) x tipo de cambio FRED")


def velas_csv(ruta: str | Path) -> pd.DataFrame:
    """CSV propio con fecha y cierre; maximo y minimo opcionales (para el ATR)."""
    df = pd.read_csv(ruta)
    cols = {c.lower().strip(): c for c in df.columns}

    def buscar(*nombres):
        return next((cols[n] for n in nombres if n in cols), None)

    c_fecha = buscar("fecha", "date", "datetime", "time")
    c_cierre = buscar("cierre", "close", "ultimo", "último", "precio", "price")
    if not c_fecha or not c_cierre:
        raise ErrorDatos(f"{ruta}: necesito columnas de fecha y de cierre")
    salida = pd.DataFrame({
        "open": pd.to_numeric(df[buscar("apertura", "open")], errors="coerce") if buscar("apertura", "open") else np.nan,
        "high": pd.to_numeric(df[buscar("maximo", "máximo", "high")], errors="coerce") if buscar("maximo", "máximo", "high") else np.nan,
        "low": pd.to_numeric(df[buscar("minimo", "mínimo", "low")], errors="coerce") if buscar("minimo", "mínimo", "low") else np.nan,
        "close": pd.to_numeric(df[c_cierre], errors="coerce"),
        "volume": np.nan,
    })
    fechas = pd.to_datetime(df[c_fecha])
    if fechas.dt.tz is not None:
        fechas = fechas.dt.tz_convert(ZONA).dt.tz_localize(None)
    salida.index = pd.DatetimeIndex(fechas)
    salida = salida[salida["close"].notna()].sort_index()
    salida = salida[~salida.index.duplicated(keep="last")]
    paso = pd.Series(salida.index).diff().median()
    vela = min(VELAS, key=lambda v: abs(VELAS[v] - (paso.total_seconds() if pd.notna(paso) else 86_400)))
    return completar(salida, vela, fuente=f"CSV {Path(ruta).name}")


def completar(df: pd.DataFrame, vela: str, fuente: str) -> pd.DataFrame:
    """
    Rellena las velas que faltan con el ultimo cierre (sin operaciones el precio
    no cambia) para que no haya huecos en las medias moviles. Cuenta cuantas
    rellena en ``attrs["huecos"]``.
    """
    if df.empty:
        raise ErrorDatos("No hay velas")
    freq = {"1d": "D", "4h": "4h", "1h": "h"}[vela]
    completo = pd.date_range(df.index[0], df.index[-1], freq=freq)
    faltan = completo.difference(df.index)
    out = df.reindex(completo)
    out["close"] = out["close"].ffill()
    for col in ("open", "high", "low"):
        if df[col].notna().any():
            out[col] = out[col].fillna(out["close"])
    if "volume" in out:
        out["volume"] = out["volume"].fillna(0.0)
    out.attrs.update(huecos=int(len(faltan)), vela=vela, fuente=fuente)
    return out


# ---------------------------------------------------------------------------
# 5. Indicadores
# ---------------------------------------------------------------------------

def atr(velas: pd.DataFrame, n: int = 14) -> pd.Series:
    """
    Average True Range de Wilder. Rango verdadero = max(H - L, |H - C_ant|, |L - C_ant|).
    Sin maximos y minimos (solo cierres) usa |C - C_ant|, que lo subestima un poco.
    """
    c = velas["close"].astype("float64")
    if {"high", "low"} <= set(velas.columns) and velas[["high", "low"]].notna().all().all():
        h, low = velas["high"].astype("float64"), velas["low"].astype("float64")
        previo = c.shift(1)
        tr = pd.concat([h - low, (h - previo).abs(), (low - previo).abs()], axis=1).max(axis=1)
    else:
        tr = c.diff().abs()
    return mt.ema(tr, n, seed="sma", alpha=1.0 / n)


def indicadores(velas: pd.DataFrame, cfg: mt.MACDConfig = mt.MACDConfig()) -> pd.DataFrame:
    close = velas["close"].astype("float64")
    d = mt.macd(close, cfg)
    d["ppo"] = 100.0 * (d["ma_fast"] - d["ma_slow"]) / d["ma_slow"]
    d["ppo_senal"] = mt.moving_average(d["ppo"], cfg.signal, cfg.kind, cfg.seed)
    d["ppo_hist"] = d["ppo"] - d["ppo_senal"]
    d["rsi14"] = mt.rsi(close, 14, "wilder")
    d["atr14"] = atr(velas, 14)
    d["atr_pct"] = 100.0 * d["atr14"] / close
    d["caida_pct"] = 100.0 * (close / close.cummax() - 1.0)
    d.attrs.update(velas.attrs)
    return d


def cruces_ppo(d: pd.DataFrame) -> pd.DataFrame:
    marco = pd.DataFrame({"close": d["close"], "macd": d["ppo"], "signal": d["ppo_senal"], "hist": d["ppo_hist"]})
    ev = mt.crossovers(marco, tol=1e-9)  # el PPO no depende del nivel del precio
    return ev[ev["evento"].isin(["cruce_alcista", "cruce_bajista"])].reset_index(drop=True)


def posicion_ppo(d: pd.DataFrame, retraso: int = 1) -> pd.Series:
    """
    True en las velas en que la estrategia de cruces tiene bitcoin. Un cruce al
    alza visto al cierre de la vela t se compra al cierre de t + retraso; uno a
    la baja se vende igual. Empieza en pesos y espera el primer cruce al alza.
    """
    estado = pd.Series(np.nan, index=d.index)
    for _, e in cruces_ppo(d).iterrows():
        estado.loc[e["fecha"]] = 1.0 if e["evento"] == "cruce_alcista" else 0.0
    estado = estado.ffill().fillna(0.0).astype(bool)
    return estado.shift(retraso, fill_value=False)


def periodos_por_cruce(d: pd.DataFrame) -> float:
    n = int(cruces_ppo(d).shape[0])
    return float(d["ppo_hist"].notna().sum()) / n if n else float("nan")


def volatilidad_diaria(close: pd.Series, vela: str = "1d", dias: int = 365) -> float:
    """Desviacion estandar de los rendimientos logaritmicos, llevada a un dia, en el ultimo ano."""
    por_dia = 86_400 / VELAS[vela]
    r = np.log(close.astype("float64")).diff().dropna()
    r = r.iloc[-int(dias * por_dia):]
    return float(r.std() * math.sqrt(por_dia))


def dias_para_recuperar(costo: float, sigma_dia: float) -> float:
    """Tiempo en que el movimiento tipico de bitcoin (una desviacion estandar) iguala el costo: (costo/sigma)^2."""
    return (costo / sigma_dia) ** 2 if sigma_dia > 0 else float("inf")


def tiempo_legible(dias: float) -> str:
    if not math.isfinite(dias):
        return "n/d"
    if dias < 1:
        return f"{dias * 24:.1f} h"
    if dias < 60:
        return f"{dias:.1f} días"
    return f"{dias / 30.44:.1f} meses"


# ---------------------------------------------------------------------------
# 6. Simulacion
# ---------------------------------------------------------------------------

def fechas_semanales(indice: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """La primera vela y despues la primera vela de cada bloque de 7 dias."""
    fechas, proxima = [], indice[0]
    for t in indice:
        if t >= proxima:
            fechas.append(t)
            while proxima <= t:
                proxima += pd.Timedelta(days=7)
    return fechas


def simular(precios: pd.Series, costos: Costos, estrategia: str, *, semanal: float, capital: float,
            posicion: pd.Series | None = None) -> pd.DataFrame:
    """
    Recorre las velas y devuelve, por vela: valor si vendieras ese dia (con
    costos), valor a precio medio, dinero puesto, bitcoin, pesos, costos pagados
    hasta ese momento y numero de operaciones.
    """
    if estrategia not in ESTRATEGIAS:
        raise ValueError(f"estrategia desconocida: {estrategia}")
    if estrategia == "cruces" and posicion is None:
        raise ValueError("la estrategia de cruces necesita la posicion")
    p = precios.astype("float64").to_numpy()
    compras = set(fechas_semanales(precios.index)) if estrategia == "semanal" else set()
    quiere = posicion.reindex(precios.index).fillna(False).to_numpy(bool) if posicion is not None else None
    btc = efectivo = aportado = pagado = 0.0
    ops = 0
    filas = np.zeros((len(p), 7))
    for i, (t, precio) in enumerate(zip(precios.index, p)):
        if estrategia == "mantener" and i == 0:
            aportado = capital
            recibido = costos.btc_por_pesos(capital, precio)
            pagado += capital - recibido * precio
            btc, ops = recibido, 1
        elif estrategia == "semanal" and t in compras:
            aportado += semanal
            recibido = costos.btc_por_pesos(semanal, precio)
            pagado += semanal - recibido * precio
            btc += recibido
            ops += 1
        elif estrategia == "cruces":
            if i == 0:
                aportado = efectivo = capital
            if quiere[i] and efectivo > 0:
                recibido = costos.btc_por_pesos(efectivo, precio)
                pagado += efectivo - recibido * precio
                btc, efectivo, ops = btc + recibido, 0.0, ops + 1
            elif not quiere[i] and btc > 0:
                pesos = costos.pesos_por_btc(btc, precio)
                pagado += btc * precio - pesos
                efectivo, btc, ops = efectivo + pesos, 0.0, ops + 1
        filas[i] = (efectivo + costos.pesos_por_btc(btc, precio), efectivo + btc * precio,
                    aportado, btc, efectivo, pagado, ops)
    return pd.DataFrame(filas, index=precios.index,
                        columns=["valor", "mercado", "aportado", "btc", "efectivo", "costos", "operaciones"])


def resumir(curva: pd.DataFrame) -> dict:
    f = curva.iloc[-1]
    relativo = curva["valor"] / curva["aportado"].where(curva["aportado"] > 0) - 1.0
    return {
        "valor_final": float(f["valor"]),
        "aportado": float(f["aportado"]),
        "rendimiento": float(f["valor"] / f["aportado"] - 1.0),
        # Lo pagado en cada operacion mas lo que costaria vender todo al final.
        "costos": float(f["costos"] + (f["mercado"] - f["valor"])),
        "operaciones": int(f["operaciones"]),
        "btc_final": float(f["btc"]),
        "peor_momento": float(relativo.min()),
        "dias_con_btc": float((curva["btc"] > 0).mean()),
    }


def correr(d: pd.DataFrame, costos: list[Costos], *, semanal: float, capital: float | None,
           retraso: int = 1) -> tuple[dict[str, dict[str, pd.DataFrame]], float]:
    """Simula las tres estrategias con cada plataforma y sin costos. Devuelve las curvas y el capital usado."""
    precios = d["close"]
    if capital is None:
        capital = semanal * len(fechas_semanales(precios.index))
    posicion = posicion_ppo(d, retraso)
    curvas: dict[str, dict[str, pd.DataFrame]] = {}
    for estrategia in ESTRATEGIAS:
        curvas[estrategia] = {c.nombre: simular(precios, c, estrategia, semanal=semanal, capital=capital,
                                                posicion=posicion) for c in [*costos, SIN_COSTOS]}
    return curvas, capital


def tabla_resultados(curvas: dict[str, dict[str, pd.DataFrame]]) -> pd.DataFrame:
    filas = []
    for estrategia, por_plataforma in curvas.items():
        for nombre, curva in por_plataforma.items():
            filas.append({"estrategia": estrategia, "plataforma": nombre, **resumir(curva)})
    return pd.DataFrame(filas)


# ---------------------------------------------------------------------------
# 7. Graficas
# ---------------------------------------------------------------------------

FONDO, TINTA, TINTA2, REJILLA = "#fcfcfb", "#0b0b0b", "#52514e", "#e2e1dc"
# Paleta categorica validada para daltonismo en pares adyacentes (orden fijo).
PALETA = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
GRIS = "#8f8e88"


def colores_plataformas(nombres: list[str]) -> dict[str, str]:
    """El color sigue a la plataforma en todas las graficas, en el orden de plataformas.toml."""
    colores = {n: PALETA[i % len(PALETA)] for i, n in enumerate(n for n in nombres if n != SIN_COSTOS.nombre)}
    colores[SIN_COSTOS.nombre] = GRIS
    return colores


def _ejes(ax) -> None:
    ax.set_facecolor(FONDO)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    for lado in ("left", "bottom"):
        ax.spines[lado].set_color("#b9b8b1")
    ax.tick_params(colors=TINTA2, labelsize=9)
    ax.grid(color=REJILLA, lw=0.8)
    ax.set_axisbelow(True)


def _pesos(x: float, _=None) -> str:
    if abs(x) >= 1e6:
        return f"${x / 1e6:,.2f} M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:,.0f} mil"
    return f"${x:,.0f}"


def _ticks_log(vmin: float, vmax: float) -> list[float]:
    """Marcas redondas (1, 2 y 5, o mas densas si hace falta) entre vmin y vmax."""
    for multiplos in ((1, 2, 5), (1, 1.5, 2, 3, 5, 7), (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 9)):
        marcas = [m * 10 ** e for e in range(int(math.floor(math.log10(vmin))) - 1,
                                             int(math.ceil(math.log10(vmax))) + 1)
                  for m in multiplos if vmin <= m * 10 ** e <= vmax]
        if len(marcas) >= 4:
            return marcas
    return marcas


def _figura(*args, **kwargs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(*args, **kwargs)
    fig.patch.set_facecolor(FONDO)
    return plt, fig, ax


def graficar_indicadores(d: pd.DataFrame, titulo: str, ruta: str | Path) -> None:
    import matplotlib.ticker as mticker
    plt, fig, axs = _figura(5, 1, figsize=(12, 12.5), sharex=True,
                            gridspec_kw={"height_ratios": [3.0, 2.0, 1.4, 1.2, 1.4], "hspace": 0.12})
    a1, a2, a3, a4, a5 = axs
    for ax in axs:
        _ejes(ax)
    a1.plot(d.index, d["close"], color="#3a3935", lw=1.2, label="Cierre")
    a1.plot(d.index, d["ma_fast"], color="#1f5fa8", lw=1.3, label="EMA 12")
    a1.plot(d.index, d["ma_slow"], color="#b3362b", lw=1.3, label="EMA 26")
    a1.set_yscale("log")
    bajo = float(min(d["close"].min(), d["ma_slow"].min(skipna=True)))
    alto = float(max(d["close"].max(), d["ma_slow"].max(skipna=True)))
    a1.yaxis.set_major_locator(mticker.FixedLocator(_ticks_log(bajo, alto)))
    a1.yaxis.set_major_formatter(mticker.FuncFormatter(_pesos))
    a1.yaxis.set_minor_locator(mticker.NullLocator())
    a1.set_ylabel("BTC/MXN (escala log)", color=TINTA2)
    a1.set_title(titulo, color=TINTA, fontsize=12, loc="left")
    a1.legend(loc="upper left", frameon=False, fontsize=9)

    paso = pd.Series(d.index).diff().median()
    colores = np.where(d["ppo_hist"] >= 0, "#2e7d4f", "#b3362b")
    a2.bar(d.index, d["ppo_hist"], width=paso * 0.8, color=colores, alpha=0.55, label="Histograma")
    a2.plot(d.index, d["ppo"], color="#1f5fa8", lw=1.2, label="PPO")
    a2.plot(d.index, d["ppo_senal"], color="#d08a00", lw=1.2, label="Señal (9)")
    a2.axhline(0, color="0.45", lw=0.8)
    a2.set_ylabel("PPO (%)", color=TINTA2)
    a2.legend(loc="upper left", frameon=False, fontsize=9, ncol=3)

    a3.plot(d.index, d["rsi14"], color="#6250d6", lw=1.1)
    for nivel, estilo in ((80, "-"), (20, "-"), (70, ":"), (30, ":")):
        a3.axhline(nivel, color="#b3362b" if nivel > 50 else "#2e7d4f", lw=0.9, ls=estilo)
    a3.set_ylim(0, 100)
    a3.set_yticks([20, 50, 80])
    a3.set_ylabel("RSI 14", color=TINTA2)
    a3.text(1.0, 0.97, "bandas 80/20 (punteadas: 70/30)", transform=a3.transAxes, ha="right",
            va="top", fontsize=8, color=TINTA2)

    a4.plot(d.index, d["atr_pct"], color="#3a3935", lw=1.1)
    a4.set_ylabel("ATR 14 (% del precio)", color=TINTA2)

    caida = 100.0 * (d["close"] / d["close"].cummax() - 1.0)  # contra el maximo dentro de la ventana
    a5.fill_between(d.index, caida, 0, color="#e34948", alpha=0.35, lw=0)
    a5.plot(d.index, caida, color="#b3362b", lw=0.9)
    a5.set_ylabel("Caída desde el\nmáximo del\nperiodo (%)", color=TINTA2)
    fig.align_ylabels(axs)
    fig.savefig(ruta, dpi=130, bbox_inches="tight", facecolor=FONDO)
    plt.close(fig)


def graficar_costos(costos: list[Costos], sigma_dia: float, ruta: str | Path) -> None:
    orden = sorted(costos, key=lambda c: c.ida_vuelta, reverse=True)  # el mas barato queda arriba
    plt, fig, ax = _figura(figsize=(11, 1.0 + 0.62 * len(orden)))
    _ejes(ax)
    ax.grid(axis="y", visible=False)
    partes = [("comision_compra", "Comisión de compra", PALETA[0]),
              ("spread", "Spread (compra contra venta)", PALETA[1]),
              ("comision_venta", "Comisión de venta", PALETA[2])]
    y = np.arange(len(orden))
    izquierda = np.zeros(len(orden))
    for llave, etiqueta, color in partes:
        valores = np.array([100 * c.componentes()[llave] for c in orden])
        ax.barh(y, valores, left=izquierda, color=color, edgecolor=FONDO, linewidth=2, height=0.62,
                label=etiqueta)
        izquierda += valores
    total_max = izquierda.max() if len(izquierda) else 1
    for yi, c, total in zip(y, orden, izquierda):
        ax.text(total + total_max * 0.012, yi,
                f"{100 * c.ida_vuelta:.2f}%  ·  {tiempo_legible(dias_para_recuperar(c.ida_vuelta, sigma_dia))}",
                va="center", fontsize=9.5, color=TINTA)
    ax.set_yticks(y, [c.nombre + ("  *" if not c.spread_medido else "") for c in orden], fontsize=10,
                  color=TINTA)
    ax.set_xlim(0, total_max * 1.32)
    ax.set_xlabel("Costo de comprar y vender de inmediato (% del monto)", color=TINTA2)
    ax.set_title("Lo que se queda cada plataforma en una compra y venta", color=TINTA, fontsize=12, loc="left",
                 pad=30)
    ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=3, frameon=False, fontsize=9)
    notas = [f"Tiempo: lo que suele tardar bitcoin en moverse una desviación estándar igual al costo, "
             f"con la volatilidad del último año ({100 * sigma_dia:.1f}% diaria)."]
    if any(not c.spread_medido for c in orden):
        notas.append("* Spread sin medir: el total solo incluye comisiones.")
    fig.text(0.01, -0.02, "\n".join(notas), fontsize=8.5, color=TINTA2, va="top")
    fig.savefig(ruta, dpi=130, bbox_inches="tight", facecolor=FONDO)
    plt.close(fig)


def graficar_valor_final(tabla: pd.DataFrame, colores: dict[str, str], ruta: str | Path) -> None:
    nombres = list(colores)
    nombres = [n for n in nombres if n != SIN_COSTOS.nombre] + [SIN_COSTOS.nombre]
    estrategias = list(ESTRATEGIAS)
    plt, fig, ax = _figura(figsize=(12, 6.2))
    _ejes(ax)
    ax.grid(axis="x", visible=False)
    ancho = 0.8 / len(nombres)
    for j, nombre in enumerate(nombres):
        valores = [float(tabla.loc[(tabla["estrategia"] == e) & (tabla["plataforma"] == nombre),
                                   "valor_final"].iloc[0]) for e in estrategias]
        xs = np.arange(len(estrategias)) - 0.4 + ancho * (j + 0.5)
        ax.bar(xs, valores, width=ancho * 0.92, color=colores[nombre], label=nombre,
               hatch="//" if nombre == SIN_COSTOS.nombre else None, edgecolor=FONDO, linewidth=1.5)
        for x, v in zip(xs, valores):
            ax.text(x, v, _pesos(v).replace(" mil", "k").replace(" M", "M"), ha="center", va="bottom",
                    fontsize=7.5, color=TINTA)
    aportado = float(tabla["aportado"].iloc[0])
    ax.axhline(aportado, color=TINTA, lw=1.0, ls="--", label=f"Dinero puesto ({_pesos(aportado)})")
    ax.set_xticks(range(len(estrategias)), [ESTRATEGIAS[e] for e in estrategias], fontsize=10, color=TINTA)
    import matplotlib.ticker as mticker
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_pesos))
    ax.set_ylim(0, max(tabla["valor_final"].max(), aportado) * 1.22)
    ax.set_ylabel("Lo que recibirías al vender al final", color=TINTA2)
    ax.set_title("Valor final de cada estrategia con los costos de cada plataforma", color=TINTA,
                 fontsize=12, loc="left")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=min(len(nombres), 4), frameon=False,
              fontsize=9)
    fig.savefig(ruta, dpi=130, bbox_inches="tight", facecolor=FONDO)
    plt.close(fig)


def graficar_curvas(curvas: dict[str, dict[str, pd.DataFrame]], colores: dict[str, str], ruta: str | Path,
                    relativo: bool = False, nota: str = "") -> None:
    """
    Una fila por estrategia y una linea por plataforma. Con ``relativo`` muestra
    cuanto valor se lleva cada plataforma contra no pagar costos, en %.
    """
    import matplotlib.ticker as mticker
    plt, fig, axs = _figura(len(curvas), 1, figsize=(12, 3.3 * len(curvas) + (0.6 if len(curvas) == 1 else 0)),
                            sharex=True, **({"gridspec_kw": {"hspace": 0.28}} if len(curvas) > 1 else {}))
    for ax, (estrategia, por_plataforma) in zip(np.atleast_1d(axs), curvas.items()):
        _ejes(ax)
        base = por_plataforma[SIN_COSTOS.nombre]["valor"]
        for nombre, curva in por_plataforma.items():
            if relativo:
                if nombre == SIN_COSTOS.nombre:
                    continue
                serie = 100.0 * (curva["valor"] / base - 1.0)
                ax.plot(serie.index, serie, color=colores[nombre], lw=1.4, label=nombre)
            else:
                estilo = dict(ls="--", lw=1.1) if nombre == SIN_COSTOS.nombre else dict(lw=1.4)
                ax.plot(curva.index, curva["valor"], color=colores[nombre], label=nombre, **estilo)
        if relativo:
            ax.axhline(0, color="0.45", lw=0.8)
            ax.set_ylabel("Contra no pagar\ncostos (%)", color=TINTA2)
        else:
            aportado = next(iter(por_plataforma.values()))["aportado"]
            ax.plot(aportado.index, aportado, color=TINTA, lw=1.0, ls=":", label="Dinero puesto")
            ax.yaxis.set_major_formatter(mticker.FuncFormatter(_pesos))
            ax.set_ylabel("Si vendieras\nese día", color=TINTA2)
        ax.set_title(ESTRATEGIAS[estrategia], color=TINTA, fontsize=11, loc="left")
    primero = np.atleast_1d(axs)[0]
    primero.legend(loc="upper left", frameon=False, fontsize=9, ncol=3)
    fig.align_ylabels(np.atleast_1d(axs))
    if nota:
        fig.text(0.01, -0.01, nota, fontsize=8.5, color=TINTA2, va="top")
    fig.savefig(ruta, dpi=130, bbox_inches="tight", facecolor=FONDO)
    plt.close(fig)


def graficar_cotizaciones(costos: list[Costos], referencia: float, momento: pd.Timestamp,
                          colores: dict[str, str], ruta: str | Path) -> None:
    """Precio efectivo de compra y de venta de cada plataforma contra el precio medio de referencia."""
    orden = sorted(costos, key=lambda c: c.ida_vuelta)
    plt, fig, ax = _figura(figsize=(11, 1.2 + 0.6 * len(orden)))
    _ejes(ax)
    ax.grid(axis="y", visible=False)
    compras = [100 * ((1 + c.prima_compra) / (1 - c.comision_compra) - 1) for c in orden]
    ventas = [100 * ((1 - c.descuento_venta) * (1 - c.comision_venta) - 1) for c in orden]
    for yi, (c, compra, venta) in enumerate(zip(orden, compras, ventas)):
        color = colores.get(c.nombre, GRIS)
        ax.plot([venta, compra], [yi, yi], color=color, lw=2.2)
        ax.plot([venta], [yi], "o", ms=8, color=FONDO, mec=color, mew=2.2)
        ax.plot([compra], [yi], "o", ms=8, color=color, mec=FONDO, mew=1.5)
        ax.text(compra + 0.05, yi, f"  compras a +{compra:.2f}%  ·  vendes a {venta:.2f}%"
                + ("  (spread sin medir)" if not c.spread_medido else ""), va="center", fontsize=9, color=TINTA)
    ax.axvline(0, color=TINTA, lw=1.0)
    ax.set_yticks(range(len(orden)), [c.nombre for c in orden], fontsize=10, color=TINTA)
    rango = max(max(compras) - min(ventas), 1.0)
    ax.set_xlim(min(ventas) - 0.15 * rango, max(compras) + 0.75 * rango)
    ax.invert_yaxis()  # la mas barata arriba, como en costos.png
    ax.set_xlabel(f"Diferencia contra el precio medio de referencia, {referencia:,.0f} MXN (%), "
                  "con comisiones", color=TINTA2)
    ax.set_title(f"Precio efectivo de compra (relleno) y de venta (hueco), {momento:%Y-%m-%d %H:%M} UTC",
                 color=TINTA, fontsize=12, loc="left")
    fig.savefig(ruta, dpi=130, bbox_inches="tight", facecolor=FONDO)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 8. Reporte
# ---------------------------------------------------------------------------

def _pct(x: float, dec: int = 2) -> str:
    return f"{100 * x:.{dec}f}%"


def tabla_costos(costos: list[Costos], plataformas: list[Plataforma], sigma_dia: float) -> pd.DataFrame:
    fuentes = {clave(p.nombre): p.fuente for p in plataformas}
    filas = []
    for c in sorted(costos, key=lambda c: c.ida_vuelta):
        filas.append({
            "Plataforma": c.nombre,
            "Comisión compra": _pct(c.comision_compra),
            "Comisión venta": _pct(c.comision_venta),
            "Spread": _pct(c.spread, 3) if c.spread_medido else "sin medir",
            "Origen del spread": c.origen_spread,
            "Compra y venta": _pct(c.ida_vuelta),
            "Tiempo para recuperarlo": tiempo_legible(dias_para_recuperar(c.ida_vuelta, sigma_dia)),
            "Fuente": fuentes.get(clave(c.nombre), ""),
        })
    return pd.DataFrame(filas)


def hallazgos(tabla: pd.DataFrame, costos: list[Costos], semanal: float, d: pd.DataFrame) -> list[str]:
    """Frases que salen directo de los numeros de la simulacion."""
    salida: list[str] = []
    por_nombre = {c.nombre: c for c in costos}
    medidos = [c for c in costos if c.spread_medido]
    if len(medidos) >= 2:
        barato = min(medidos, key=lambda c: c.ida_vuelta)
        caro = max(medidos, key=lambda c: c.ida_vuelta)
        salida.append(f"Comprar y vender de inmediato cuesta desde {_pct(barato.ida_vuelta)} en {barato.nombre} "
                      f"hasta {_pct(caro.ida_vuelta)} en {caro.nombre}.")
    for c in costos:
        if not c.spread_medido:
            salida.append(f"En {c.nombre} cuesta al menos {_pct(c.ida_vuelta)} solo en comisiones; falta sumar "
                          f"su spread, que se mide con `btc_macd.py cotiza --manual`.")
    semanal_t = tabla[(tabla.estrategia == "semanal") & (tabla.plataforma != SIN_COSTOS.nombre)]
    if len(semanal_t) >= 2:
        mejor = semanal_t.loc[semanal_t.btc_final.idxmax()]
        for _, fila in semanal_t.iterrows():
            if fila.plataforma == mejor.plataforma:
                continue
            extra = mejor.btc_final / fila.btc_final - 1
            if clave(fila.plataforma) == clave("Mercado Pago") or fila.btc_final == semanal_t.btc_final.min():
                aviso = ("; con su spread la diferencia sería mayor"
                         if not por_nombre[fila.plataforma].spread_medido else "")
                salida.append(f"Comprando {_pesos(semanal)} cada semana, con {mejor.plataforma} habrías juntado "
                              f"{mejor.btc_final:.6f} BTC, {_pct(extra, 1)} más que con {fila.plataforma}"
                              f" ({fila.btc_final:.6f} BTC){aviso}.")
    cruces_t = tabla[tabla.estrategia == "cruces"]
    if not cruces_t.empty:
        ops = int(cruces_t.operaciones.max())
        base = float(cruces_t.loc[cruces_t.plataforma == SIN_COSTOS.nombre, "valor_final"].iloc[0])
        peor = cruces_t[cruces_t.plataforma != SIN_COSTOS.nombre].sort_values("valor_final").iloc[0]
        salida.append(f"Los cruces del PPO pidieron {ops} operaciones. Con los costos de {peor.plataforma} "
                      f"el valor final queda {_pct(1 - peor.valor_final / base, 1)} abajo del que tendrías "
                      f"sin pagar costos.")
    for nombre in [c.nombre for c in costos]:
        filas = tabla[tabla.plataforma == nombre].set_index("estrategia")
        if len(filas) == len(ESTRATEGIAS):
            mejor = filas.valor_final.idxmax()
            salida.append(f"Con {nombre}, la estrategia que terminó con más dinero fue "
                          f"«{ESTRATEGIAS[mejor]}» ({_pesos(filas.loc[mejor, 'valor_final'])}).")
            break
    frec = periodos_por_cruce(d)
    if math.isfinite(frec):
        vela = d.attrs.get("vela", "1d")
        unidad = {"1d": "días", "4h": "velas de 4 horas", "1h": "horas"}[vela]
        comparado = ("tan seguido como el azar o más" if frec < mt.PERIODOS_RUIDO
                     else "menos seguido que el azar")
        salida.append(f"El PPO de bitcoin cruzó su señal una vez cada {frec:.1f} {unidad}. Una caminata "
                      f"aleatoria cruza cada {mt.PERIODOS_RUIDO} periodos, así que cruza {comparado}.")
    return salida


def escribir_reporte(ruta: str | Path, *, d: pd.DataFrame, tabla: pd.DataFrame, costos: list[Costos],
                     plataformas: list[Plataforma], avisos: list[str], semanal: float, capital: float,
                     sigma_dia: float, desde: pd.Timestamp, retraso: int) -> None:
    vela = d.attrs.get("vela", "1d")
    fuente = d.attrs.get("fuente", "")
    periodo = d.loc[desde:]
    periodo.attrs.update(d.attrs)
    n_semanas = len(fechas_semanales(periodo.index))
    resultados = tabla.copy()
    resultados["estrategia"] = resultados["estrategia"].map(ESTRATEGIAS)
    resultados = pd.DataFrame({
        "Estrategia": resultados["estrategia"],
        "Plataforma": resultados["plataforma"],
        "Valor final": resultados["valor_final"].map(lambda v: f"${v:,.0f}"),
        "Rendimiento": resultados["rendimiento"].map(lambda v: _pct(v, 1)),
        "Costos pagados": resultados["costos"].map(lambda v: f"${int(round(v)):,}"),
        "Operaciones": resultados["operaciones"],
        "BTC al final": resultados["btc_final"].map(lambda v: f"{v:.6f}"),
        "Peor momento": resultados["peor_momento"].map(lambda v: _pct(v, 1)),
    })
    lineas = [
        "# Bitcoin con pesos: costos por plataforma y tres estrategias",
        "",
        f"Datos: {fuente}, {NOMBRE_VELA[vela]}, del {periodo.index[0]:%Y-%m-%d} al {periodo.index[-1]:%Y-%m-%d} "
        f"({len(periodo):,} velas). Volatilidad del último año: {_pct(sigma_dia, 1)} diaria. "
        f"Generado el {pd.Timestamp.now(tz=ZONA):%Y-%m-%d %H:%M} (hora de la Ciudad de México) con `btc_macd.py`.",
        "",
        "Ejercicio académico: describe costos y lo que habría pasado en el pasado. No es una recomendación "
        "de inversión.",
        "",
        "## Costos por plataforma",
        "",
        "![Costos por plataforma](costos.png)",
        "",
        tabla_costos(costos, plataformas, sigma_dia).to_markdown(index=False),
        "",
        "*Compra y venta* es lo que pierdes si compras y vendes de inmediato. *Tiempo para recuperarlo* es lo "
        "que suele tardar bitcoin en moverse una desviación estándar igual a ese costo: (costo / volatilidad "
        "diaria)².",
        "",
        "## Tres estrategias con el mismo dinero",
        "",
        f"Compra fija: {_pesos(semanal)} cada 7 días, {n_semanas} compras, {_pesos(capital)} en total. Las otras "
        f"dos ponen esos {_pesos(capital)} desde el primer día: una compra y mantiene; la otra compra en cada cruce "
        f"del PPO al alza y vende en cada cruce a la baja, al cierre de la vela siguiente (retraso de {retraso}). "
        "El valor es lo que recibirías si vendieras, ya con costos. *Peor momento* es la mayor pérdida contra "
        "el dinero puesto en algún punto del camino.",
        "",
        "![Valor final](valor_final.png)",
        "",
        resultados.to_markdown(index=False),
        "",
        "![Valor en el tiempo](curvas.png)",
        "",
        "![Lo que se llevan los costos en los cruces](costo_acumulado.png)",
        "",
        "Al mantener y en la compra semanal, cada peso paga la compra una vez y la venta una vez, así que la "
        "diferencia contra no pagar costos es la misma de una compra y venta inmediata. En los cruces se paga "
        "en cada operación y se acumula.",
        "",
        "## Lo que dicen los números",
        "",
        *[f"- {h}" for h in hallazgos(tabla, costos, semanal, periodo)],
        "",
        "## Indicadores",
        "",
        "![Indicadores](indicadores.png)",
        "",
        "PPO (MACD en % de la EMA lenta), RSI de 14 con bandas 80/20, ATR de 14 en % del precio y caída "
        "desde el máximo. Un RSI arriba de 70 es normal en las tendencias fuertes de bitcoin, por eso las "
        "bandas van en 80/20.",
    ]
    if d.attrs.get("huecos"):
        lineas += ["", f"Se rellenaron {d.attrs['huecos']} velas sin operaciones con el cierre anterior."]
    if avisos:
        lineas += ["", "## Avisos", "", *[f"- {a}" for a in avisos]]
    Path(ruta).write_text("\n".join(lineas) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 9. Comandos
# ---------------------------------------------------------------------------

def _imprimir_cotizaciones(costos: list[Costos], monto: float, referencia: float,
                           etiqueta: str = "Precio medio de referencia") -> None:
    print(f"\n{etiqueta}: {referencia:,.2f} MXN por BTC")
    print(f"{'Plataforma':28} {'Compra y venta':>15} {'Spread':>9}   BTC por {_pesos(monto):>9}   "
          f"Te regresan al vender al instante")
    for c in sorted(costos, key=lambda c: c.ida_vuelta):
        btc = c.btc_por_pesos(monto, referencia)
        regreso = c.pesos_por_btc(btc, referencia)
        spread = _pct(c.spread, 3) if c.spread_medido else "sin medir"
        print(f"{c.nombre:28} {_pct(c.ida_vuelta):>15} {spread:>9}   {btc:>15.8f}   ${regreso:,.2f}")


def _momento_local(texto: str) -> pd.Timestamp:
    """AAAA-MM-DD HH:MM en hora de la Ciudad de Mexico (o con zona explicita), en UTC."""
    try:
        t = pd.Timestamp(texto)
    except (TypeError, ValueError):
        raise ErrorDatos(f"--momento {texto!r}: escribe AAAA-MM-DD HH:MM, hora de la Ciudad de México") from None
    return (t.tz_localize(ZONA) if t.tzinfo is None else t).tz_convert("UTC")


def cmd_cotiza(args: argparse.Namespace, obtener: Callable = obtener_json,
               ahora: pd.Timestamp | None = None) -> int:
    plataformas, ref = cargar_plataformas(args.plataformas)
    api_ref = ref.get("api", "bitso")
    manuales = [(nombre, numero(compra), numero(venta)) for nombre, compra, venta in args.manual or []]
    conocidas = {clave(p.nombre) for p in plataformas}
    avisos = [f"{nombre} no está en {Path(args.plataformas).name}: se guarda la cotización, pero "
              "agrega la plataforma con sus comisiones para compararla."
              for nombre, _, _ in manuales if clave(nombre) not in conocidas]
    registro = Path(args.cotizaciones)

    if args.momento:
        # Precios que viste antes (una captura de pantalla): el libro de ese momento ya no se puede
        # leer, asi que la referencia es el ultimo precio de Bitso en ese minuto y no se leen libros.
        if not manuales:
            raise ErrorDatos("--momento sirve para precios anotados con --manual")
        if api_ref != "bitso":
            raise ErrorDatos("--momento usa las velas de un minuto de Bitso: pon [referencia] api = \"bitso\"")
        momento = _momento_local(args.momento)
        if momento > (pd.Timestamp.now(tz="UTC") if ahora is None else ahora):
            raise ErrorDatos(f"--momento {args.momento}: ese minuto todavía no pasa")
        referencia = precio_bitso_en(momento, obtener)
        guardar_cotizaciones([Cotizacion(n, c, v, referencia, momento, "manual") for n, c, v in manuales],
                             registro)
        costos, avisos2 = resolver_costos(plataformas, {}, leer_cotizaciones(registro))
        local = momento.tz_convert(ZONA)
        _imprimir_cotizaciones(costos, args.monto, referencia,
                               f"Último precio operado en Bitso el {local:%Y-%m-%d} a las {local:%H:%M} "
                               "(Ciudad de México)")
        for a in avisos + avisos2:
            print(f"  aviso: {a}")
        print(f"\nCotizaciones guardadas en {registro}.")
        return 0

    apis = {p.api for p in plataformas if p.spread == "libro" and p.api} | {api_ref}
    libros, avisos_red = leer_libros(apis, obtener) if not args.sin_red else ({}, [])
    avisos += avisos_red
    if api_ref not in libros:
        print(f"Sin precio de referencia ({api_ref}): {'; '.join(avisos_red) or 'modo sin red'}", file=sys.stderr)
        return 1
    referencia = medio(libros[api_ref])
    momento = libros[api_ref][2]
    nuevas: list[Cotizacion] = []
    for p in plataformas:
        if p.spread == "libro" and p.api in libros:
            # Una plataforma con API se mide dentro de su propio libro; una app, contra la referencia.
            bid, ask, t = libros[p.api]
            nuevas.append(Cotizacion(p.nombre, ask, bid, (bid + ask) / 2, t, "api"))
    for nombre, compra, venta in manuales:
        nuevas.append(Cotizacion(nombre, compra, venta, referencia, pd.Timestamp.now(tz="UTC"), "manual"))
    guardar_cotizaciones(nuevas, registro)
    # Esta corrida manda: las plataformas con libro usan el libro de ahora; las demas, todo lo anotado.
    historial = leer_cotizaciones(registro)
    costos, avisos2 = resolver_costos(plataformas, libros, historial)
    _imprimir_cotizaciones(costos, args.monto, referencia)
    for a in avisos + avisos2:
        print(f"  aviso: {a}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    colores = colores_plataformas([p.nombre for p in plataformas])
    graficar_cotizaciones(costos, referencia, momento, colores, out / "cotizacion.png")
    print(f"\nCotizaciones guardadas en {registro}. Gráfica en {out / 'cotizacion.png'}")
    return 0


def cmd_analiza(args: argparse.Namespace, obtener: Callable = obtener_json,
                obtener_txt: Callable = obtener_texto) -> int:
    plataformas, ref = cargar_plataformas(args.plataformas)
    registro = leer_cotizaciones(args.cotizaciones)
    apis = {p.api for p in plataformas if p.spread == "libro" and p.api}
    libros, avisos = leer_libros(apis, obtener) if not args.sin_red else ({}, [])
    costos, avisos2 = resolver_costos(plataformas, libros, registro)
    avisos += avisos2

    hasta = pd.Timestamp(args.hasta) if args.hasta else pd.Timestamp.now(tz=ZONA).tz_localize(None).normalize()
    desde = pd.Timestamp(args.desde) if args.desde else hasta - pd.DateOffset(years=3)
    por_dia = 86_400 / VELAS[args.vela]
    inicio = desde - pd.Timedelta(days=math.ceil(CALENTAMIENTO / por_dia))
    if args.csv:
        velas = velas_csv(args.csv)
    elif args.fuente == "coinmetrics":
        if args.vela != "1d":
            raise ErrorDatos("CoinMetrics solo tiene velas diarias")
        velas = velas_coinmetrics(inicio, hasta, obtener_txt)
    else:
        velas = velas_bitso(inicio, hasta, args.vela, obtener)
    atributos = dict(velas.attrs)
    velas = velas.loc[:hasta + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)]
    velas.attrs.update(atributos)
    d = indicadores(velas)
    if d.loc[desde:].empty:
        raise ErrorDatos("No hay velas después de --desde")
    desde = d.loc[desde:].index[0]
    periodo = d.loc[desde:]
    periodo.attrs.update(d.attrs)
    sigma = volatilidad_diaria(d["close"], args.vela)
    curvas, capital = correr(periodo, costos, semanal=args.semanal, capital=args.capital, retraso=args.retraso)
    tabla = tabla_resultados(curvas)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    colores = colores_plataformas([c.nombre for c in costos])
    titulo = (f"BTC/MXN · {NOMBRE_VELA[args.vela]} · {d.attrs.get('fuente', '')} · "
              f"{periodo.index[0]:%Y-%m-%d} a {periodo.index[-1]:%Y-%m-%d}")
    graficar_indicadores(periodo, titulo, out / "indicadores.png")
    graficar_costos(costos, sigma, out / "costos.png")
    graficar_valor_final(tabla, colores, out / "valor_final.png")
    graficar_curvas(curvas, colores, out / "curvas.png")
    graficar_curvas({"cruces": curvas["cruces"]}, colores, out / "costo_acumulado.png", relativo=True,
                    nota="Al mantener y en la compra semanal la diferencia no cambia con el tiempo: es un solo "
                         "viaje de compra y venta, el de costos.png.")
    tabla.to_csv(out / "estrategias.csv", index=False, float_format="%.6f")
    tabla_costos(costos, plataformas, sigma).to_csv(out / "costos.csv", index=False)
    periodo.to_csv(out / "serie.csv", float_format="%.6f", index_label="fecha")
    escribir_reporte(out / "reporte.md", d=d, tabla=tabla, costos=costos, plataformas=plataformas,
                     avisos=avisos, semanal=args.semanal, capital=capital, sigma_dia=sigma, desde=desde,
                     retraso=args.retraso)

    print(f"{len(periodo):,} {NOMBRE_VELA[args.vela]}, {periodo.index[0]:%Y-%m-%d} a {periodo.index[-1]:%Y-%m-%d} "
          f"({d.attrs.get('fuente', '')}). Volatilidad diaria: {_pct(sigma, 1)}.")
    for h in hallazgos(tabla, costos, args.semanal, periodo):
        print(f"- {h}")
    for a in avisos:
        print(f"  aviso: {a}")
    print(f"Reporte, gráficas y CSV en {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="btc_macd.py", description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="comando", required=True, metavar="COMANDO")
    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument("--plataformas", default=str(RAIZ / "plataformas.toml"),
                       help="archivo con comisiones y spreads (default: plataformas.toml)")
    comun.add_argument("--cotizaciones", default="cotizaciones.csv",
                       help="registro de cotizaciones medidas (default: cotizaciones.csv)")
    comun.add_argument("--sin-red", action="store_true", help="no leer libros en vivo")

    c = sub.add_parser("cotiza", parents=[comun], help="precios de compra y venta ahora mismo")
    c.add_argument("--manual", nargs=3, action="append", metavar=("NOMBRE", "COMPRA", "VENTA"),
                   help='precios que ves en una app, p. ej. --manual "Mercado Pago" 1561000 1498000; '
                        'también como los escribe la app, 1.503.231,35')
    c.add_argument("--momento", metavar="\"AAAA-MM-DD HH:MM\"",
                   help="cuándo viste esos precios, hora de la Ciudad de México (de una captura, por ejemplo); "
                        "se miden contra el último precio de Bitso en ese minuto y no se leen libros")
    c.add_argument("--monto", type=float, default=10_000, help="pesos para el ejemplo (default: 10000)")
    c.add_argument("--out", default="resultados_btc", help="carpeta de la gráfica (default: resultados_btc)")

    a = sub.add_parser("analiza", parents=[comun], help="indicadores, costos y tres estrategias")
    a.add_argument("--desde", help="fecha de inicio, AAAA-MM-DD (default: tres años antes de --hasta)")
    a.add_argument("--hasta", help="última fecha, AAAA-MM-DD (default: hoy; la vela en curso se descarta)")
    a.add_argument("--vela", choices=list(VELAS), default="1d", help="duración de cada vela (default: 1d)")
    a.add_argument("--fuente", choices=["bitso", "coinmetrics"], default="bitso",
                   help="de dónde salen las velas (default: bitso)")
    a.add_argument("--csv", help="tus propias velas: columnas fecha y cierre, máximo y mínimo opcionales")
    a.add_argument("--semanal", type=float, default=500, help="pesos de la compra semanal (default: 500)")
    a.add_argument("--capital", type=float,
                   help="pesos para mantener y para los cruces (default: lo mismo que suman las compras semanales)")
    a.add_argument("--retraso", type=int, default=1, help="velas entre el cruce y la operación (default: 1)")
    a.add_argument("--out", default="resultados_btc", help="carpeta de resultados (default: resultados_btc)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.comando == "cotiza":
            return cmd_cotiza(args)
        return cmd_analiza(args)
    except ErrorDatos as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
