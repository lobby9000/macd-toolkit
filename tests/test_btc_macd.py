"""
Pruebas de btc_macd.py. No usan internet: las respuestas de Bitso y Binance son
grabadas (tests/datos) o fabricadas aqui.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import btc_macd as b

DATOS = Path(__file__).resolve().parent / "datos"


def leer(nombre):
    return json.loads((DATOS / nombre).read_text(encoding="utf-8"))


def costos(cc=0.015, cv=0.015, a=0.01, bb=0.01, nombre="Prueba"):
    return b.Costos(nombre, cc, cv, a, bb, "prueba")


def serie(valores, inicio="2025-01-01"):
    return pd.Series(np.asarray(valores, dtype=float), index=pd.date_range(inicio, periods=len(valores), freq="D"))


def senoide(n=400, periodo=40):
    t = np.arange(n)
    return serie(100_000 * (1 + 0.1 * np.sin(2 * np.pi * t / periodo)))


# ---------------------------------------------------------------- costos

def test_compra_y_venta_con_comision_y_spread():
    c = costos()
    btc = c.btc_por_pesos(10_000, 1_000_000)
    assert btc == pytest.approx(10_000 * 0.985 / (1_000_000 * 1.01))
    assert c.pesos_por_btc(btc, 1_000_000) == pytest.approx(btc * 1_000_000 * 0.99 * 0.985)
    assert c.ida_vuelta == pytest.approx(1 - 0.985 * 0.985 * 0.99 / 1.01)
    assert c.spread == pytest.approx(0.02)


def test_los_componentes_suman_el_costo_de_ida_y_vuelta():
    for c in (costos(), costos(0.0078, 0.0078, 0.0002, 0.0002), costos(0, 0, 0.03, 0.01), b.SIN_COSTOS):
        assert sum(c.componentes().values()) == pytest.approx(c.ida_vuelta, abs=1e-15)
    assert b.SIN_COSTOS.ida_vuelta == 0


def test_tiempo_para_recuperar_el_costo():
    # 1.56% de ida y vuelta con 3% de volatilidad diaria: (0.0156 / 0.03)^2 dias = 6.5 horas
    dias = b.dias_para_recuperar(0.0156, 0.03)
    assert dias * 24 == pytest.approx(6.49, abs=0.01)
    assert b.tiempo_legible(dias) == "6.5 h"
    assert b.tiempo_legible(3.0) == "3.0 días"
    assert b.tiempo_legible(float("inf")) == "n/d"


# ---------------------------------------------------------------- cotizaciones

def test_cotizacion_mide_prima_descuento_y_spread():
    q = b.Cotizacion("Mercado Pago", 1_030, 980, 1_000, pd.Timestamp("2026-10-14", tz="UTC"), "manual")
    assert q.prima_compra == pytest.approx(0.03)
    assert q.descuento_venta == pytest.approx(0.02)
    assert q.spread == pytest.approx(0.05)
    with pytest.raises(b.ErrorDatos):
        b.Cotizacion("Al reves", 980, 1_030, 1_000, pd.Timestamp("2026-10-14", tz="UTC"), "manual")


def test_libro_de_bitso_con_respuesta_grabada():
    bid, ask, momento = b.libro_bitso(lambda url: leer("bitso_ticker.json"))
    assert (bid, ask) == (1_533_290.0, 1_533_910.0)
    assert momento == pd.Timestamp("2026-10-14T00:42:39Z")
    with pytest.raises(b.ErrorDatos):
        b.libro_bitso(lambda url: {"success": False, "error": {"code": "0201"}})


def test_libro_de_binance_prueba_el_segundo_servidor():
    pedidas = []

    def obtener(url):
        pedidas.append(url)
        if "api.binance.com" in url:
            raise b.ErrorDatos("451 desde este pais")
        return leer("binance_book.json")

    bid, ask, _ = b.libro_binance(obtener)
    assert (bid, ask) == (1_512_826.0, 1_514_472.0)
    assert [u.split("/api/")[0] for u in pedidas] == list(b.BINANCE)


def test_registro_de_cotizaciones_ida_y_vuelta(tmp_path):
    ruta = tmp_path / "cotizaciones.csv"
    q = [b.Cotizacion("Mercado Pago", 1_561_000, 1_498_000, 1_533_600, pd.Timestamp("2026-10-14T21:20:14Z"), "manual"),
         b.Cotizacion("Bitso, orden de mercado", 1_533_910, 1_533_290, 1_533_600,
                      pd.Timestamp("2026-10-14T00:42:39Z"), "api")]
    b.guardar_cotizaciones(q[:1], ruta)
    b.guardar_cotizaciones(q[1:], ruta)  # se agrega sin repetir el encabezado
    leidas = b.leer_cotizaciones(ruta)
    assert [x.plataforma for x in leidas] == [x.plataforma for x in q]
    assert leidas[0].spread == pytest.approx(q[0].spread)
    assert ruta.read_text(encoding="utf-8").count("momento_utc") == 1
    assert b.leer_cotizaciones(tmp_path / "no-existe.csv") == []


# ---------------------------------------------------------------- plataformas

def test_plataformas_del_repositorio():
    plataformas, ref = b.cargar_plataformas(b.RAIZ / "plataformas.toml")
    por_nombre = {p.nombre: p for p in plataformas}
    assert ref["api"] == "bitso"
    mp = por_nombre["Mercado Pago"]
    assert (mp.comision_compra, mp.comision_venta, mp.spread) == (0.015, 0.015, "por_medir")
    assert por_nombre["Bitso, orden de mercado"].comision_compra == pytest.approx(0.0078)
    assert por_nombre["Bitso, orden de mercado"].api == "bitso"
    assert all(p.fuente for p in plataformas), "cada plataforma dice de donde salen sus numeros"


@pytest.mark.parametrize("bloque, mensaje", [
    ('nombre = "X"\ncomision_compra = 1\ncolor = "azul"', "claves desconocidas"),
    ('nombre = "X"\nspread = "libro"', "necesita api"),
    ('nombre = "X"\ncomision_compra = -1', "fuera de 0 a 100"),
    ('nombre = "X"\nspread = "a veces"', "debe ser un número"),
    ('nombre = "X"\napi = "kraken"\nspread = 0', "no existe"),
])
def test_plataformas_con_errores(tmp_path, bloque, mensaje):
    ruta = tmp_path / "p.toml"
    ruta.write_text(f"[[plataforma]]\n{bloque}\n", encoding="utf-8")
    with pytest.raises(b.ErrorDatos, match=mensaje):
        b.cargar_plataformas(ruta)


def test_plataforma_repetida_por_acentos(tmp_path):
    ruta = tmp_path / "p.toml"
    ruta.write_text('[[plataforma]]\nnombre = "Bitso límite"\n[[plataforma]]\nnombre = "bitso  limite"\n',
                    encoding="utf-8")
    with pytest.raises(b.ErrorDatos, match="dos veces"):
        b.cargar_plataformas(ruta)


def test_de_donde_sale_el_spread():
    fijo = b.Plataforma("Fija", 0.006, 0.006, 0.002)
    libro = b.Plataforma("Bitso", 0.0078, 0.0078, "libro", api="bitso", spread_tipico=0.0005)
    app = b.Plataforma("Mercado Pago", 0.015, 0.015, "por_medir")
    p2p = b.Plataforma("P2P", 0.0, 0.0, "por_medir", omitir_sin_medicion=True)
    t = pd.Timestamp("2026-10-14T12:00Z")
    libros = {"bitso": (999.0, 1_001.0, t)}
    registro = [b.Cotizacion("mercado pago", 1_030, 980, 1_000, t, "manual"),
                b.Cotizacion("Mercado Pago", 1_050, 990, 1_000, t, "manual"),
                b.Cotizacion("Bitso", 1_002, 998, 1_000, t, "api")]

    c = b.costos_de(fijo, libros={}, registro=[])
    assert (c.prima_compra, c.descuento_venta) == (0.001, 0.001)
    c = b.costos_de(libro, libros=libros, registro=registro)
    assert c.prima_compra == pytest.approx(0.001) and "en vivo" in c.origen_spread
    c = b.costos_de(libro, libros={}, registro=registro)          # sin conexion: lo guardado
    assert c.prima_compra == pytest.approx(0.002) and "promedio de 1" in c.origen_spread
    c = b.costos_de(libro, libros={}, registro=[])                # sin nada: el tipico
    assert c.spread == pytest.approx(0.0005) and "típico" in c.origen_spread
    c = b.costos_de(app, libros=libros, registro=registro)        # promedio de dos, sin importar acentos
    assert c.prima_compra == pytest.approx(0.04) and c.descuento_venta == pytest.approx(0.015)
    c = b.costos_de(app, libros=libros, registro=[])
    assert not c.spread_medido and c.spread == 0
    assert b.costos_de(p2p, libros=libros, registro=[]) is None


# ---------------------------------------------------------------- velas

def test_velas_de_bitso_con_respuesta_grabada():
    muestra = leer("bitso_ohlc_muestra.json")
    velas = b.velas_bitso("2024-10-13", "2024-10-14", "1d", obtener=lambda url: muestra,
                          ahora=pd.Timestamp("2026-10-14T12:00Z"), pausa=0)
    # 1728799200000 = 2024-10-13 06:00 UTC = medianoche en la Ciudad de Mexico
    assert list(velas.index) == [pd.Timestamp("2024-10-13"), pd.Timestamp("2024-10-14")]
    assert velas.loc["2024-10-14", "close"] == 1_264_250.0
    assert velas.loc["2024-10-13", ["open", "high", "low"]].tolist() == [1_212_030.0, 1_241_830.0, 1_196_530.0]
    assert velas.attrs["fuente"].startswith("Bitso")


def _bitso_falso(precio, pedidos, faltan=()):
    """Responde /v3/ohlc con una vela diaria por dia local dentro de [start, end)."""
    def obtener(url):
        pedidos.append(url)
        q = dict(par.split("=") for par in url.split("?")[1].split("&"))
        inicio, fin = int(q["start"]), int(q["end"])
        dias = pd.date_range(pd.Timestamp(inicio, unit="ms", tz="UTC").tz_convert(b.ZONA).normalize(),
                             pd.Timestamp(fin, unit="ms", tz="UTC").tz_convert(b.ZONA), freq="D")
        filas = []
        for dia in dias:
            ms = int(dia.tz_convert("UTC").timestamp() * 1000)
            if ms >= fin or dia.tz_localize(None) in faltan:
                continue
            p = precio(dia.tz_localize(None))
            filas.append({"bucket_start_time": ms, "first_rate": str(p), "last_rate": str(p),
                          "min_rate": str(p * 0.99), "max_rate": str(p * 1.01), "volume": "1"})
        return {"success": True, "payload": filas}
    return obtener


def test_velas_de_bitso_paginan_rellenan_y_descartan_la_vela_en_curso():
    pedidos = []
    precio = lambda dia: 1_000_000 + 1_000 * (dia - pd.Timestamp("2024-01-01")).days  # noqa: E731
    obtener = _bitso_falso(precio, pedidos, faltan={pd.Timestamp("2024-06-15")})
    ahora = pd.Timestamp("2024-12-31T20:00", tz=b.ZONA)  # el 31 de diciembre todavia no cierra
    velas = b.velas_bitso("2024-01-01", "2024-12-31", "1d", obtener=obtener, ahora=ahora, pausa=0)
    assert len(pedidos) == 2, "366 dias son dos paginas de 300 velas"
    assert velas.index[0] == pd.Timestamp("2024-01-01")
    assert velas.index[-1] == pd.Timestamp("2024-12-30")
    assert velas.attrs["huecos"] == 1
    assert velas.loc["2024-06-15", "close"] == velas.loc["2024-06-14", "close"]
    assert velas.index.is_unique and (velas.index.to_series().diff().dropna() == pd.Timedelta(days=1)).all()


def test_coinmetrics_por_tipo_de_cambio():
    btc = "time,PriceUSD\n2026-01-02,100000\n2026-01-03,101000\n2026-01-04,102000\n2026-01-05,103000\n"
    fx = ("Date,Country,Exchange rate\n2026-01-01,Mexico,18.0\n2026-01-02,Mexico,18.5\n"
          "2026-01-02,Canada,1.4\n2026-01-05,Mexico,19.0\n")
    textos = {b.COINMETRICS_CSV: btc, b.TIPO_CAMBIO_CSV: fx}
    velas = b.velas_coinmetrics("2026-01-01", "2026-01-31", obtener=lambda url: textos[url])
    # el fin de semana (3 y 4 de enero) se usa el tipo de cambio del viernes 2
    assert velas["close"].tolist() == pytest.approx([1_850_000, 1_868_500, 1_887_000, 1_957_000])
    assert velas[["high", "low"]].isna().all().all()


def test_csv_propio_con_maximo_y_minimo(tmp_path):
    ruta = tmp_path / "velas.csv"
    ruta.write_text("Fecha,Cierre,Maximo,Minimo\n2025-01-01,10,11,9\n2025-01-02,12,13,10\n2025-01-04,11,12,10\n",
                    encoding="utf-8")
    velas = b.velas_csv(ruta)
    assert velas.attrs["vela"] == "1d" and velas.attrs["huecos"] == 1
    assert velas["close"].tolist() == [10, 12, 12, 11]


# ---------------------------------------------------------------- indicadores

def test_atr_de_wilder_a_mano():
    velas = pd.DataFrame({"high": [11, 12, 14, 13, 15.0], "low": [9, 10, 11, 10, 12.0],
                          "close": [10, 11, 13, 11, 14.0]}, index=pd.date_range("2025-01-01", periods=5))
    # rango verdadero: 2, max(2, 2, 0) = 2, max(3, 3, 0) = 3, max(3, 0, 3) = 3, max(3, 4, 1) = 4
    tr = [2, 2, 3, 3, 4]
    esperado = [np.nan, np.nan, np.mean(tr[:3])]
    for valor in tr[3:]:
        esperado.append((esperado[-1] * 2 + valor) / 3)
    assert b.atr(velas, n=3).tolist() == pytest.approx(esperado, nan_ok=True)


def test_atr_sin_maximos_usa_el_cambio_entre_cierres():
    velas = pd.DataFrame({"close": [10, 12, 9, 10.0], "high": np.nan, "low": np.nan},
                         index=pd.date_range("2025-01-01", periods=4))
    assert b.atr(velas, n=2).tolist() == pytest.approx([np.nan, np.nan, 2.5, 1.75], nan_ok=True)


def test_ppo_es_el_macd_entre_la_ema_lenta():
    d = b.indicadores(pd.DataFrame({"close": senoide()}))
    validos = d.dropna(subset=["ppo"])
    assert validos["ppo"].to_numpy() == pytest.approx((100 * validos["macd"] / validos["ma_slow"]).to_numpy())
    assert d["ppo_senal"].tolist() == pytest.approx(
        b.mt.ema(d["ppo"], 9).tolist(), nan_ok=True)
    assert (d["caida_pct"] <= 0).all()


def test_los_cruces_se_operan_al_cierre_siguiente():
    d = b.indicadores(pd.DataFrame({"close": senoide()}))
    alcistas = b.cruces_ppo(d).query("evento == 'cruce_alcista'")["fecha"]
    primero = alcistas.iloc[0]
    for retraso in (0, 1, 2):
        pos = b.posicion_ppo(d, retraso)
        assert pos.idxmax() == d.index[d.index.get_loc(primero) + retraso]
    assert not b.posicion_ppo(d, 1).loc[:primero].any(), "nada de comprar con un cruce que todavia no se veia"


def test_frecuencia_de_cruces_de_un_seno():
    d = b.indicadores(pd.DataFrame({"close": senoide(n=800, periodo=40)}))
    assert b.periodos_por_cruce(d) == pytest.approx(20, rel=0.05)  # dos cruces por ciclo de 40


# ---------------------------------------------------------------- simulacion

def test_mantener_sin_costos_sigue_al_precio():
    precios = serie([100, 110, 90, 120])
    curva = b.simular(precios, b.SIN_COSTOS, "mantener", semanal=0, capital=1_000)
    assert curva["valor"].tolist() == pytest.approx([1_000, 1_100, 900, 1_200])


def test_mantener_con_costos_es_un_solo_viaje():
    precios = serie([100, 110, 90, 120])
    c = costos()
    curva = b.simular(precios, c, "mantener", semanal=0, capital=1_000)
    assert curva["valor"].iloc[-1] == pytest.approx(1_200 * c.factor_ida_vuelta)
    r = b.resumir(curva)
    btc = 1_000 * (1 - c.comision_compra) / (100 * (1 + c.prima_compra))
    pagado_al_comprar = 1_000 - btc * 100
    pagado_al_vender = btc * 120 * (1 - (1 - c.descuento_venta) * (1 - c.comision_venta))
    assert r["costos"] == pytest.approx(pagado_al_comprar + pagado_al_vender)
    assert r["operaciones"] == 1


def test_compra_semanal_cada_siete_dias():
    precios = serie(np.linspace(100, 130, 30))
    c = costos(0.01, 0.01, 0.0, 0.0)
    curva = b.simular(precios, c, "semanal", semanal=500, capital=0)
    dias = [0, 7, 14, 21, 28]
    assert curva["operaciones"].iloc[-1] == len(dias)
    assert curva["aportado"].iloc[-1] == 500 * len(dias)
    btc = sum(500 * 0.99 / precios.iloc[i] for i in dias)
    assert curva["btc"].iloc[-1] == pytest.approx(btc)
    assert b.fechas_semanales(precios.index) == [precios.index[i] for i in dias]


def test_cruces_pagan_en_cada_operacion():
    d = b.indicadores(pd.DataFrame({"close": senoide(n=500, periodo=30)}))
    pos = b.posicion_ppo(d, 1)
    c = costos()
    con = b.simular(d["close"], c, "cruces", semanal=0, capital=10_000, posicion=pos)
    sin = b.simular(d["close"], b.SIN_COSTOS, "cruces", semanal=0, capital=10_000, posicion=pos)
    compras = int((pos & ~pos.shift(1, fill_value=False)).sum())
    ventas = int((~pos & pos.shift(1, fill_value=False)).sum())
    assert con["operaciones"].iloc[-1] == compras + ventas
    factor = ((1 - c.comision_compra) / (1 + c.prima_compra)) ** compras \
        * ((1 - c.descuento_venta) * (1 - c.comision_venta)) ** (ventas + int(pos.iloc[-1]))
    assert con["valor"].iloc[-1] / sin["mercado"].iloc[-1] == pytest.approx(factor)


def test_mismo_dinero_en_las_tres_estrategias():
    d = b.indicadores(pd.DataFrame({"close": senoide(n=300)}))
    curvas, capital = b.correr(d, [costos()], semanal=500, capital=None)
    assert capital == 500 * len(b.fechas_semanales(d.index))
    aportados = {e: por["Prueba"]["aportado"].iloc[-1] for e, por in curvas.items()}
    assert set(aportados.values()) == {capital}
    tabla = b.tabla_resultados(curvas)
    assert len(tabla) == 3 * 2  # tres estrategias, la plataforma y "sin costos"
    sin = tabla[tabla.plataforma == "Sin costos"].set_index("estrategia")
    con = tabla[tabla.plataforma == "Prueba"].set_index("estrategia")
    assert (con.valor_final < sin.valor_final).all()


# ---------------------------------------------------------------- comandos completos

def test_analiza_de_principio_a_fin_sin_red(tmp_path):
    rng = np.random.default_rng(3)
    dias = pd.date_range("2023-01-01", periods=700, freq="D")
    cierre = 400_000 * np.exp(np.cumsum(rng.normal(0.0008, 0.03, len(dias))))
    pd.DataFrame({"fecha": dias, "cierre": cierre}).to_csv(tmp_path / "btc.csv", index=False)
    salida = tmp_path / "resultados"
    codigo = b.main(["analiza", "--csv", str(tmp_path / "btc.csv"), "--sin-red", "--desde", "2023-08-01",
                     "--hasta", "2024-11-30", "--out", str(salida), "--cotizaciones", str(tmp_path / "c.csv")])
    assert codigo == 0
    for nombre in ("reporte.md", "indicadores.png", "costos.png", "valor_final.png", "curvas.png",
                   "costo_acumulado.png", "estrategias.csv", "costos.csv", "serie.csv"):
        assert (salida / nombre).stat().st_size > 0, nombre
    reporte = (salida / "reporte.md").read_text(encoding="utf-8")
    assert "Mercado Pago" in reporte and "sin medir" in reporte
    assert "No es una recomendación de inversión" in reporte
    tabla = pd.read_csv(salida / "estrategias.csv")
    assert set(tabla.estrategia) == set(b.ESTRATEGIAS)


def test_cotiza_mide_la_app_contra_bitso(tmp_path):
    respuestas = {f"{b.BITSO}/ticker/?book=btc_mxn": leer("bitso_ticker.json"),
                  f"{b.BINANCE[0]}/api/v3/ticker/bookTicker?symbol=BTCMXN": leer("binance_book.json")}
    args = b.build_parser().parse_args([
        "cotiza", "--manual", "Mercado Pago", "1560000", "1500000",
        "--out", str(tmp_path), "--cotizaciones", str(tmp_path / "c.csv")])
    assert b.cmd_cotiza(args, obtener=lambda url: respuestas[url]) == 0
    registro = {q.plataforma: q for q in b.leer_cotizaciones(tmp_path / "c.csv")}
    assert registro["Mercado Pago"].referencia == pytest.approx((1_533_290 + 1_533_910) / 2)
    assert registro["Binance, orden de mercado"].referencia == pytest.approx((1_512_826 + 1_514_472) / 2)
    assert (tmp_path / "cotizacion.png").exists()


def test_cotiza_sin_referencia_termina_con_error(tmp_path, capsys):
    def sin_red(url):
        raise b.ErrorDatos("sin red")
    args = b.build_parser().parse_args(["cotiza", "--out", str(tmp_path), "--cotizaciones", str(tmp_path / "c.csv")])
    assert b.cmd_cotiza(args, obtener=sin_red) == 1
    assert "Sin precio de referencia" in capsys.readouterr().err
