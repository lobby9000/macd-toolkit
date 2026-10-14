# Bitcoin con pesos: costos por plataforma y tres estrategias

Datos: Bitso, libro BTC/MXN, velas diarias, del 2023-10-14 al 2026-10-13 (1,096 velas). Volatilidad del último año: 2.0% diaria. Generado el 2026-10-14 15:26 (hora de la Ciudad de México) con `btc_macd.py`.

Ejercicio académico: describe costos y lo que habría pasado en el pasado. No es una recomendación de inversión.

## Costos por plataforma

![Costos por plataforma](costos.png)

| Plataforma                | Comisión compra   | Comisión venta   | Spread    | Origen del spread                   | Compra y venta   | Tiempo para recuperarlo   | Fuente                                                                                                                                        |
|:--------------------------|:------------------|:-----------------|:----------|:------------------------------------|:-----------------|:--------------------------|:----------------------------------------------------------------------------------------------------------------------------------------------|
| Binance, orden de mercado | 0.10%             | 0.10%            | 0.073%    | libro en vivo, 2026-10-14 21:25 UTC | 0.27%            | 0.4 h                     | Comisión spot estándar de 0.1% (criptoinforme.com, 5 de octubre de 2026; finantres.mx, 14 de octubre de 2026). Spread: libro BTC/MXN por API. |
| Bitso, orden límite       | 0.60%             | 0.60%            | 0.000%    | fijo en el archivo, 0.00%           | 1.20%            | 8.2 h                     | bitso.com/mx/fees, comisión maker con menos de 20,000 MXN al mes (consultada el 2026-10-14).                                                  |
| Bitso, orden de mercado   | 0.78%             | 0.78%            | 0.039%    | libro en vivo, 2026-10-14 21:25 UTC | 1.59%            | 14.5 h                    | bitso.com/mx/fees, comisión taker con menos de 20,000 MXN al mes (consultada el 2026-10-14). Spread: libro BTC/MXN por API.                   |
| Mercado Pago              | 1.50%             | 1.50%            | sin medir | sin medir: solo comisiones          | 2.98%            | 2.1 días                  | Comisión: Luis, en la app de Mercado Pago (octubre de 2026). Spread: por medir en la app.                                                     |

*Compra y venta* es lo que pierdes si compras y vendes de inmediato. *Tiempo para recuperarlo* es lo que suele tardar bitcoin en moverse una desviación estándar igual a ese costo: (costo / volatilidad diaria)².

## Tres estrategias con el mismo dinero

Compra fija: $500 cada 7 días, 157 compras, $78 mil en total. Las otras dos ponen esos $78 mil desde el primer día: una compra y mantiene; la otra compra en cada cruce del PPO al alza y vende en cada cruce a la baja, al cierre de la vela siguiente (retraso de 1). El valor es lo que recibirías si vendieras, ya con costos. *Peor momento* es la mayor pérdida contra el dinero puesto en algún punto del camino.

![Valor final](valor_final.png)

| Estrategia              | Plataforma                | Valor final   | Rendimiento   | Costos pagados   |   Operaciones |   BTC al final | Peor momento   |
|:------------------------|:--------------------------|:--------------|:--------------|:-----------------|--------------:|---------------:|:---------------|
| Comprar todo y mantener | Mercado Pago              | $239,641      | 205.3%        | $4,827           |             1 |       0.158653 | -3.0%          |
| Comprar todo y mantener | Bitso, orden de mercado   | $243,062      | 209.6%        | $2,586           |             1 |       0.159781 | -1.6%          |
| Comprar todo y mantener | Bitso, orden límite       | $244,040      | 210.9%        | $1,944           |             1 |       0.160102 | -1.2%          |
| Comprar todo y mantener | Binance, orden de mercado | $246,323      | 213.8%        | $443             |             1 |       0.160849 | -0.3%          |
| Comprar todo y mantener | Sin costos                | $246,995      | 214.6%        | $0               |             1 |       0.161069 | 0.0%           |
| Compra fija cada semana | Mercado Pago              | $91,906       | 17.1%         | $2,577           |           157 |       0.060846 | -21.1%         |
| Compra fija cada semana | Bitso, orden de mercado   | $93,218       | 18.7%         | $1,379           |           157 |       0.061279 | -20.0%         |
| Compra fija cada semana | Bitso, orden límite       | $93,593       | 19.2%         | $1,036           |           157 |       0.061402 | -19.7%         |
| Compra fija cada semana | Binance, orden de mercado | $94,469       | 20.3%         | $236             |           157 |       0.061688 | -18.9%         |
| Compra fija cada semana | Sin costos                | $94,727       | 20.7%         | $0               |           157 |       0.061772 | -18.7%         |
| Cruces del PPO          | Mercado Pago              | $64,247       | -18.2%        | $101,318         |            74 |       0        | -22.5%         |
| Cruces del PPO          | Bitso, orden de mercado   | $108,552      | 38.3%         | $69,650          |            74 |       0        | -1.6%          |
| Cruces del PPO          | Bitso, orden límite       | $125,939      | 60.4%         | $56,397          |            74 |       0        | -1.2%          |
| Cruces del PPO          | Binance, orden de mercado | $177,723      | 126.4%        | $15,367          |            74 |       0        | -0.3%          |
| Cruces del PPO          | Sin costos                | $196,594      | 150.4%        | $0               |            74 |       0        | 0.0%           |

![Valor en el tiempo](curvas.png)

![Lo que se llevan los costos en los cruces](costo_acumulado.png)

Al mantener y en la compra semanal, cada peso paga la compra una vez y la venta una vez, así que la diferencia contra no pagar costos es la misma de una compra y venta inmediata. En los cruces se paga en cada operación y se acumula.

## Lo que dicen los números

- Comprar y vender de inmediato cuesta desde 0.27% en Binance, orden de mercado hasta 1.59% en Bitso, orden de mercado.
- En Mercado Pago cuesta al menos 2.98% solo en comisiones; falta sumar su spread, que se mide con `btc_macd.py cotiza --manual`.
- Comprando $500 cada semana, con Binance, orden de mercado habrías juntado 0.061688 BTC, 1.4% más que con Mercado Pago (0.060846 BTC); con su spread la diferencia sería mayor.
- Los cruces del PPO pidieron 74 operaciones. Con los costos de Mercado Pago el valor final queda 67.3% abajo del que tendrías sin pagar costos.
- Con Mercado Pago, la estrategia que terminó con más dinero fue «Comprar todo y mantener» ($240 mil).
- El PPO de bitcoin cruzó su señal una vez cada 14.8 días. Una caminata aleatoria cruza cada 12 periodos, así que cruza menos seguido que el azar.

## Indicadores

![Indicadores](indicadores.png)

PPO (MACD en % de la EMA lenta), RSI de 14 con bandas 80/20, ATR de 14 en % del precio y caída desde el máximo. Un RSI arriba de 70 es normal en las tendencias fuertes de bitcoin, por eso las bandas van en 80/20.

## Avisos

- Mercado Pago: spread sin medir, el costo solo incluye sus comisiones.
- Binance P2P queda fuera: su spread no está medido (python btc_macd.py cotiza --manual "Binance P2P" COMPRA VENTA).
