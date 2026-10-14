"""
batch_macd.py
=============

Corre el analisis de ``macd_toolkit`` sobre una lista de emisoras y deja, en una
sola pasada, una grafica y un CSV por emisora mas una tabla comparativa con el
estado de todas al cierre del periodo elegido.

Uso tipico para el simulador (cierres de viernes, corte en el viernes pasado)::

    python batch_macd.py bmv_tickers.txt --freq W --asof 2026-09-18 --years 3

Solo la tabla, sin generar imagenes (mucho mas rapido)::

    python batch_macd.py bmv_tickers.txt --asof 2026-09-18 --sin-graficas

El archivo de emisoras es un .txt con un ticker por linea. Las lineas vacias y
las que empiezan con ``#`` se ignoran, y se puede poner un comentario despues
del ticker::

    FEMSAUBD.MX    # FEMSA UBD
    WALMEX.MX      # Walmart de Mexico
    # CEMEXCPO.MX  <- desactivada por ahora

Por que un script de lote y no un ciclo ``for`` en la terminal:

* Una sola llamada a yfinance por bloque de emisoras en vez de una por emisora,
  que es mas rapido y evita que Yahoo corte la conexion por exceso de peticiones.
* pandas y matplotlib se importan una vez y no una vez por emisora.
* Un ticker malo no tumba la corrida: se anota en ``fallidos.txt`` y sigue.
* Se produce la tabla comparativa, que es lo unico que de verdad se puede leer
  cuando son treinta emisoras. Treinta imagenes sueltas no se revisan.

Este es un ejercicio academico de analisis tecnico. No es una recomendacion de
inversion.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")          # sin ventana grafica: solo escribe archivos

import pandas as pd

import macd_toolkit as mk


# ---------------------------------------------------------------------------
# Lista de emisoras
# ---------------------------------------------------------------------------

def leer_tickers(path: str | Path, sufijo: str = "") -> list[str]:
    """
    Lee el .txt de emisoras. Devuelve la lista en el orden del archivo, sin
    repetidos. ``sufijo`` se agrega solo a los tickers que no traen punto, para
    no tocar los que ya vienen completos ni los de otros mercados.
    """
    tickers: list[str] = []
    for linea in Path(path).read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        t = linea.split()[0].strip().upper()
        if sufijo and "." not in t:
            t += sufijo
        if t not in tickers:
            tickers.append(t)
    if not tickers:
        raise SystemExit(f"No se encontró ningún ticker en {path}")
    return tickers


# ---------------------------------------------------------------------------
# Una emisora
# ---------------------------------------------------------------------------

def analizar(nombre: str, diario: pd.Series, args: argparse.Namespace) -> tuple[dict, pd.DataFrame]:
    """Calcula el MACD de una emisora y devuelve (fila de resumen, tabla completa)."""
    cierres = mk.resample_close(diario, args.freq, asof=args.asof,
                                keep_partial=args.incluir_parcial)
    cfg = mk.MACDConfig(args.fast, args.slow, args.signal, kind=args.kind, seed=args.seed)
    if len(cierres) < cfg.first_signal:
        raise ValueError(f"solo {len(cierres)} periodos, la señal necesita {cfg.first_signal}")

    df = mk.macd(cierres, cfg)
    df["ppo"] = mk.ppo(cierres, cfg)
    df["rsi14"] = mk.rsi(cierres, 14, "wilder")

    ultimo = df.dropna(subset=["hist"]).iloc[-1]
    previo = df.dropna(subset=["hist"]).iloc[-2]

    eventos = mk.crossovers(df)
    cruces = eventos[eventos["evento"].isin(["cruce_alcista", "cruce_bajista"])]
    if len(cruces):
        ult_cruce = cruces.iloc[-1]
        fecha_cruce = pd.Timestamp(ult_cruce["fecha"])
        tipo_cruce = ult_cruce["evento"].replace("cruce_", "")
        # cuantos periodos de la serie han pasado desde el cruce
        periodos_desde = int((df.index > fecha_cruce).sum())
    else:
        fecha_cruce, tipo_cruce, periodos_desde = pd.NaT, "sin cruce", -1

    h, h0 = ultimo["hist"], previo["hist"]
    fila = {
        "ticker": nombre,
        "periodos": len(cierres),
        "ultima_fecha": df.index[-1].date(),
        "cierre": round(float(ultimo["close"]), 4),
        "macd": round(float(ultimo["macd"]), 4),
        "senal": round(float(ultimo["signal"]), 4),
        "histograma": round(float(h), 4),
        "hist_previo": round(float(h0), 4),
        "macd_vs_senal": "arriba" if h > 0 else "abajo",
        "macd_vs_cero": "arriba" if ultimo["macd"] > 0 else "abajo",
        "histograma_va": "creciendo" if abs(h) > abs(h0) else "reduciendose",
        "ppo_pct": round(float(ultimo["ppo"]), 4),
        "rsi14": round(float(ultimo["rsi14"]), 2) if pd.notna(ultimo["rsi14"]) else None,
        "ultimo_cruce": tipo_cruce,
        "fecha_cruce": fecha_cruce.date() if pd.notna(fecha_cruce) else "",
        "periodos_desde_cruce": periodos_desde,
        "cruces_totales": len(cruces),
    }
    return fila, df


# ---------------------------------------------------------------------------
# Corrida completa
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    tickers = leer_tickers(args.lista, sufijo=args.sufijo)
    out_dir = Path(args.out)
    (out_dir / "graficas").mkdir(parents=True, exist_ok=True)
    (out_dir / "datos").mkdir(parents=True, exist_ok=True)

    print(f"{len(tickers)} emisoras por analizar. Frecuencia {args.freq}, "
          f"corte {args.asof or 'último periodo cerrado'}.")

    # --- descarga en bloques ----------------------------------------------
    series: dict[str, pd.Series] = {}
    fallidos: list[tuple[str, str]] = []
    if args.csv_dir:
        for t in tickers:
            ruta = Path(args.csv_dir) / f"{t}.csv"
            if ruta.exists():
                series[t] = mk.load_csv(ruta)
            else:
                fallidos.append((t, f"no existe {ruta}"))
    else:
        bloques = [tickers[i:i + args.bloque] for i in range(0, len(tickers), args.bloque)]
        for i, bloque in enumerate(bloques, 1):
            print(f"  descargando bloque {i}/{len(bloques)} ({len(bloque)} emisoras)...")
            series.update(mk.download_many(bloque, years=args.years,
                                           column=args.column, asof=args.asof))
            if i < len(bloques):
                time.sleep(args.pausa)
        for t in tickers:
            if t not in series:
                fallidos.append((t, "sin datos en Yahoo (revisa el ticker)"))

    # --- emisoras rezagadas y reintento ------------------------------------
    # Yahoo a veces entrega una emisora sin sus ultimas sesiones. El hueco cambia
    # de emisora entre descargas, asi que casi siempre se arregla volviendo a
    # pedir solo esas. La referencia es el dia mas reciente que alcanzo
    # cualquiera de la lista: si 30 emisoras llegan al viernes y 5 se quedan en
    # el martes, esas 5 son las rezagadas.
    def ultimo_dia(s: pd.Series) -> pd.Timestamp:
        return pd.Timestamp(s.index[-1])

    rezagadas: list[str] = []
    # El reintento solo aplica cuando se descarga. Con --csv-dir no hay a quien
    # volver a preguntar, pero la deteccion y el marcado se hacen igual.
    if series and not args.csv_dir:
        for intento in range(1, args.reintentos + 1):
            referencia = max(ultimo_dia(s) for s in series.values())
            rezagadas = sorted(t for t, s in series.items() if ultimo_dia(s) < referencia)
            if not rezagadas:
                break
            print(f"  {len(rezagadas)} emisoras sin datos hasta {referencia.date()}: "
                  f"{', '.join(rezagadas)}")
            if intento > args.reintentos:
                break
            print(f"  reintento {intento}/{args.reintentos} solo para esas...")
            time.sleep(args.pausa)
            nuevas = mk.download_many(rezagadas, years=args.years,
                                      column=args.column, asof=args.asof)
            mejoro = False
            for t, s in nuevas.items():
                if t in series and ultimo_dia(s) > ultimo_dia(series[t]):
                    series[t] = s
                    mejoro = True
            if not mejoro:
                print("  el reintento no trajo datos más recientes.")
                break
    if series:
        referencia = max(ultimo_dia(s) for s in series.values())
        rezagadas = sorted(t for t, s in series.items() if ultimo_dia(s) < referencia)
        if args.solo_completas and rezagadas:
            for t in rezagadas:
                fallidos.append((t, f"datos solo hasta {ultimo_dia(series[t]).date()}, "
                                    f"se esperaba {referencia.date()}"))
                series.pop(t)
            print(f"  descartadas por --solo-completas: {', '.join(rezagadas)}")
            rezagadas = []

    # --- calculo y salidas -------------------------------------------------
    filas = []
    for t in tickers:
        if t not in series:
            continue
        try:
            fila, df = analizar(t, series[t], args)
        except Exception as exc:
            fallidos.append((t, str(exc)))
            print(f"  {t}: {exc}")
            continue

        fila["datos_al_dia"] = "no" if t in rezagadas else "sí"
        filas.append(fila)
        tag = t.replace(".", "_")
        mostrado = df.tail(args.show)
        df.to_csv(out_dir / "datos" / f"{tag}_completo.csv", float_format="%.6f")
        mostrado.to_csv(out_dir / "datos" / f"{tag}_ultimos{args.show}.csv", float_format="%.6f")

        if not args.sin_graficas:
            etiquetas = (f"{args.kind.upper()} {args.fast}", f"{args.kind.upper()} {args.slow}")
            titulo = (f"{t}   MACD({args.fast},{args.slow},{args.signal})   "
                      f"{args.freq}   al {df.index[-1].date()}")
            periodo = {"D": "día", "W": "semana", "M": "mes"}[args.freq]
            mk.plot_macd(mostrado, titulo, out_dir / "graficas" / f"{tag}.png",
                         labels=etiquetas,
                         resumen=mk.resumen_tecnico(df, periodo))
        print(f"  {t}: {fila['periodos']} periodos, último {fila['ultima_fecha']}, "
              f"hist {fila['histograma']:+.4f} ({fila['histograma_va']})")

    if not filas:
        raise SystemExit("Ninguna emisora pudo analizarse. Revisa la lista y la conexión.")

    # --- tabla comparativa -------------------------------------------------
    resumen = pd.DataFrame(filas)
    # Lo más útil arriba: los cruces más recientes primero, y entre iguales,
    # el histograma más fuerte.
    resumen = resumen.sort_values(
        by=["periodos_desde_cruce", "histograma"],
        ascending=[True, False],
        key=lambda s: s.where(s >= 0, 10**6) if s.name == "periodos_desde_cruce" else s,
    ).reset_index(drop=True)

    resumen.to_csv(out_dir / "resumen.csv", index=False)
    columnas_md = ["ticker", "ultima_fecha", "datos_al_dia", "cierre", "macd", "senal",
                   "histograma", "histograma_va", "macd_vs_cero", "ppo_pct", "rsi14",
                   "ultimo_cruce", "fecha_cruce", "periodos_desde_cruce"]
    aviso = ""
    if rezagadas:
        aviso = (f"**Atención:** {len(rezagadas)} emisoras traen datos incompletos "
                 f"({', '.join(rezagadas)}). Su columna `datos_al_dia` dice no y sus "
                 f"indicadores están calculados sobre un cierre anterior al de las demás, "
                 f"así que no son comparables con el resto de la tabla.\n\n")
    encabezado = (
        f"# Resumen MACD({args.fast},{args.slow},{args.signal}) "
        f"frecuencia {args.freq}\n\n"
        f"Corte: {args.asof or 'último periodo cerrado'}. "
        f"Emisoras analizadas: {len(resumen)}. "
        f"Ordenadas por cruce más reciente.\n\n"
        + aviso +
        f"Ejercicio académico de análisis técnico, no es recomendación de inversión.\n\n"
    )
    try:
        tabla = resumen[columnas_md].to_markdown(index=False)
    except ImportError:
        # to_markdown necesita el paquete tabulate. Si falta, no vale la pena
        # perder toda la corrida por el formato de una tabla.
        tabla = resumen[columnas_md].to_string(index=False)
        print("Aviso: falta el paquete 'tabulate', resumen.md quedó en texto simple.")
    (out_dir / "resumen.md").write_text(encabezado + tabla + "\n", encoding="utf-8")

    if fallidos:
        (out_dir / "fallidos.txt").write_text(
            "\n".join(f"{t}\t{motivo}" for t, motivo in fallidos) + "\n", encoding="utf-8")

    # --- cierre ------------------------------------------------------------
    print()
    print(f"Listo: {len(resumen)} emisoras analizadas, {len(fallidos)} sin resultado.")
    alcistas = resumen[resumen["macd_vs_senal"] == "arriba"]
    print(f"MACD por encima de su señal: {len(alcistas)} de {len(resumen)}.")
    recientes = resumen[(resumen["periodos_desde_cruce"] >= 0) &
                        (resumen["periodos_desde_cruce"] <= 3)]
    if len(recientes):
        print(f"Cruces en los últimos 3 periodos: "
              f"{', '.join(recientes['ticker'] + ' (' + recientes['ultimo_cruce'] + ')')}")
    if rezagadas:
        print()
        print(f"ATENCIÓN: {len(rezagadas)} emisoras quedaron con datos incompletos "
              f"después de {args.reintentos} reintento(s):")
        print(f"  {', '.join(rezagadas)}")
        print("  Sus indicadores usan un cierre anterior al de las demás y no son")
        print("  comparables. Vuelve a correr más tarde, o usa --solo-completas")
        print("  para sacarlas de la tabla.")
    else:
        print("Todas las emisoras llegan al mismo último cierre.")
    print(f"Archivos en {out_dir.resolve()}")
    if fallidos:
        print(f"Revisa fallidos.txt: {', '.join(t for t, _ in fallidos)}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="MACD en lote sobre una lista de emisoras",
        epilog="Ejemplo: python batch_macd.py bmv_tickers.txt --freq W --asof 2026-09-18 --years 3")
    p.add_argument("lista", help="Archivo .txt con un ticker por línea")
    p.add_argument("--freq", choices=["D", "W", "M"], default="W")
    p.add_argument("--asof", default=None,
                   help="Fecha de corte AAAA-MM-DD, p. ej. 2026-09-18")
    p.add_argument("--years", type=float, default=3.0,
                   help="Años de historia a descargar (default 3, suficiente para semanal)")
    p.add_argument("--show", type=int, default=26, help="Periodos a graficar y exportar")
    p.add_argument("--fast", type=int, default=12)
    p.add_argument("--slow", type=int, default=26)
    p.add_argument("--signal", type=int, default=9)
    p.add_argument("--kind", choices=["ema", "sma"], default="ema")
    p.add_argument("--seed", choices=["sma", "first"], default="sma")
    p.add_argument("--column", choices=["Close", "Adj Close"], default="Close")
    p.add_argument("--sufijo", default="",
                   help="Se agrega a los tickers sin punto, p. ej. .MX para la BMV")
    p.add_argument("--bloque", type=int, default=20,
                   help="Cuántas emisoras se piden por llamada a Yahoo")
    p.add_argument("--pausa", type=float, default=1.0,
                   help="Segundos de espera entre bloques")
    p.add_argument("--csv-dir", dest="csv_dir", default=None,
                   help="Carpeta con TICKER.csv propios, en vez de descargar")
    p.add_argument("--reintentos", type=int, default=2,
                   help="Veces que se vuelve a pedir a Yahoo solo las emisoras "
                        "que quedaron sin sus últimas sesiones (default 2)")
    p.add_argument("--solo-completas", dest="solo_completas", action="store_true",
                   help="Saca de la tabla las emisoras que sigan incompletas "
                        "después de los reintentos")
    p.add_argument("--sin-graficas", dest="sin_graficas", action="store_true",
                   help="Solo tablas, sin generar los PNG")
    p.add_argument("--incluir-parcial", dest="incluir_parcial", action="store_true",
                   help="Conserva el periodo en curso aunque todavía no cierre")
    p.add_argument("--out", default="resultados_lote")
    return p


if __name__ == "__main__":
    run(build_parser().parse_args())
