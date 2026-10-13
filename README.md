# Minero de Bitcoin en solitario

Un minero de Bitcoin escrito desde cero en Python (sin dependencias) que se
conecta a **un solo pool de minería en solitario**. Si tu PC encuentra un
bloque, la recompensa completa (3,125 BTC más las comisiones de las
transacciones) va **directamente a tu wallet**: el pool la escribe en el propio
bloque, a tu dirección, y nunca pasa por sus manos.

> **Antes de nada, lee la sección de expectativas.** Minar Bitcoin en
> solitario con un PC es una lotería con unas probabilidades minúsculas. Este
> programa no promete ganancias y te muestra en todo momento tus
> probabilidades reales.

## Expectativas reales

La red de Bitcoin calcula unos **950 EH/s** (950 trillones de hashes por
segundo), casi todo con ASICs, máquinas diseñadas solo para esto. Cuánto tardarías
de media en encontrar un bloque, con la dificultad de octubre de 2026 (~132,7 T):

| Equipo | Velocidad aprox. | Tiempo medio hasta un bloque | Probabilidad en 1 año |
|---|---|---|---|
| Tu Ryzen 7 9800X3D con este minero | ~10–15 MH/s | ~1.200–1.800 millones de años | 1 entre ~1.500 millones |
| CPU con un minero en C optimizado | ~100 MH/s | ~180 millones de años | 1 entre 180 millones |
| GPU de gama alta | ~5 GH/s | ~3,6 millones de años | 1 entre 3,6 millones |
| Bitaxe (minero ASIC doméstico) | ~1 TH/s | ~18.000 años | 1 entre 18.000 |
| Antminer S21 | ~200 TH/s | ~90 años | 1 entre 91 |
| Toda la red | ~950 EH/s | 10 minutos | — |

- **Que la luz te salga gratis no cambia nada:** el límite es la velocidad, y
  ningún programa puede hacer que una CPU encuentre bloques más a menudo.
- **Ethereum no se puede minar** desde septiembre de 2022 (*The Merge*): ahora
  funciona con *proof of stake* y no existen pools de minería de ETH.
- Aun así, la lotería a veces toca: en julio de 2026 un Bitaxe de ~1 TH/s
  encontró el bloque 957.382 a través de public-pool.io y se llevó ~3,14 BTC.
  A tu velocidad, la probabilidad es unas 100.000 veces menor que la suya.
- Si quieres **ganar algo de verdad** con tu PC, mira [MONERO.md](MONERO.md):
  tu 9800X3D es de los mejores procesadores para minar Monero (unos 13–15 $
  al mes con la luz gratis, pagados directamente a tu wallet).

## Qué hace este minero

- Se conecta a **un solo pool** en solitario (por defecto `public-pool.io:3333`,
  sin comisión) con el protocolo Stratum v1, y reconecta solo si se cae.
- **Comprueba que el pool te paga a ti:** lee la transacción *coinbase* de
  cada trabajo y te dice qué parte de la recompensa iría a tu dirección. Si el
  bloque no te paga, si te deja menos del 95 % o si el pool cambia el reparto
  a mitad de sesión, te avisa con un mensaje de ATENCIÓN que se repite en cada
  estadística.
- Usa todos los hilos de tu CPU con **prioridad baja**, así que el PC sigue
  respondiendo (navegar, vídeo…). En juegos que exprimen la CPU puedes notar
  menos FPS: para el minero mientras juegas.
- En Windows evita que el PC se **suspenda** mientras mina (la pantalla puede
  apagarse), y desactiva la "edición rápida" de la consola, que congelaba el
  programa al hacer clic en la ventana.
- Valida tu dirección (bc1q…, bc1p…, 1…, 3…; también si pegas un enlace
  `bitcoin:` de un QR) y **se niega a aceptar una frase semilla o una clave
  privada** si la pegas por error.
- Se protege de pools rotos o maliciosos: ignora mensajes malformados,
  trabajos con una dificultad de red imposible y avalanchas de shares, y nunca
  se cierra por un mensaje raro (reconecta).
- Muestra cada 30 s tu velocidad, las shares aceptadas, tu mejor share y el
  tiempo medio que tardarías en encontrar un bloque.

## Instalación en Windows

1. **Instala Python** (3.9 o más reciente; recomendado 3.12 o superior) desde
   <https://www.python.org/downloads/>. En el instalador marca
   **"Add python.exe to PATH"**.
2. **Descarga este repositorio**: botón verde *Code* → *Download ZIP*, y
   descomprímelo en una carpeta.
3. **Consigue una dirección de Bitcoin tuya**, la de "Recibir" de una wallet
   que controles tú (por ejemplo Sparrow, BlueWallet o Electrum). Empieza por
   `bc1q…`. No uses la dirección de depósito de un exchange: algunos no
   aceptan pagos que vienen directamente de un bloque minado.
4. **Mide tu velocidad**: doble clic en `benchmark.bat`. No se conecta a
   ningún sitio.
5. **Empieza a minar**: doble clic en `iniciar_minero.bat`. La primera vez te
   pedirá tu dirección y la guardará en `minero_config.json` para las
   siguientes veces.

Para parar, pulsa `Ctrl+C` o cierra la ventana. Windows preguntará
"¿Desea terminar el trabajo por lotes (S/N)?": da igual lo que contestes, el
resumen ya se ha mostrado.

### Desde la línea de comandos (Windows, Linux o macOS)

```
py minero.py --wallet bc1qTU_DIRECCION
py minero.py --wallet bc1qTU_DIRECCION --pool solo.ckpool.org:3333
py minero.py --benchmark
```

En Linux y macOS usa `python3` en lugar de `py`.

### Opciones

| Opción | Para qué sirve | Por defecto |
|---|---|---|
| `-w`, `--wallet` | Tu dirección pública de Bitcoin | se pregunta la primera vez |
| `-o`, `--pool` | Pool en solitario (`host:puerto`, admite `stratum+tcp://` y `stratum+ssl://`) | `public-pool.io:3333` |
| `--trabajador` | Nombre de este equipo en el pool | nombre del PC |
| `-t`, `--hilos` | Procesos de minado | todos los hilos de la CPU |
| `--prioridad` | `baja` (PC usable) o `normal` | `baja` |
| `--dificultad` | Dificultad de share que se pide al pool al conectar (`0` = no pedir ninguna) | `1` |
| `--intervalo` | Segundos entre estadísticas | `30` |
| `--benchmark` / `--segundos` | Medir la velocidad sin conectarse | 20 s |
| `--no-guardar` | No guardar la configuración en `minero_config.json` | — |
| `--permitir-suspension` | Dejar que Windows suspenda el PC aunque esté minando | — |

## Qué verás en pantalla

```
[18:02:11] Conectando a public-pool.io:3333...
[18:02:12] Conectado. Minando para bc1q... (trabajador 'JasonPC').
[18:02:12] Dificultad de la red: 132.70 T (un bloque necesita un hash al menos así de difícil).
[18:02:12] Comprobado: si encuentras este bloque, 3.14012345 BTC (100.0 % de la recompensa) van DIRECTAMENTE a tu dirección bc1q...
[18:02:42] Velocidad: 12.31 MH/s | Shares: 0 aceptadas, 0 rechazadas
[18:02:42] Tiempo medio hasta un bloque: 1,5 mil millones de años | Probabilidad en 1 año: 1 entre 1,5 mil millones
```

- **Share:** un hash que cumple una dificultad pequeña que fija el pool. Sirve
  para demostrar que estás trabajando, pero en solitario **no se paga nada por
  las shares**: solo cuenta encontrar un bloque entero.
- **Mejor share:** la dificultad del mejor hash que has encontrado. Para
  ganar un bloque haría falta una share con la dificultad de la red (~132,7 T).

## Si encuentras un bloque

1. Verás `¡¡¡BLOQUE ENCONTRADO!!!` y el minero lo envía al pool, que lo publica en
   la red.
2. La recompensa ya está dentro del bloque, en una salida a tu dirección. No
   hay que reclamarla ni esperar a ningún pago del pool.
3. Bitcoin exige **100 confirmaciones** (unas 17 horas) antes de poder gastar
   una recompensa de minería. Puedes buscar el bloque en <https://mempool.space>.

## Si quieres jugar a la lotería de Bitcoin con más boletos

Un **Bitaxe Gamma** (~1,1–1,2 TH/s, ~20 W, unos 120–220 €) es unas 100.000
veces más rápido que tu PC con este minero, y el bloque de julio de 2026 lo
encontró uno de estos aparatos. Aun así, de media tardaría unos 15.000 años en
encontrar un bloque.

Si compras uno, cuidado: su firmware viene apuntando a `public-pool.io:3333`,
pero con una **dirección de ejemplo que no es tuya**. Cámbiala por la tuya
tanto en el pool principal como en el de respaldo, o cualquier premio sería
para otra persona.

## Pools en solitario

| Pool | Dirección | Comisión | Notas |
|---|---|---|---|
| public-pool.io | `public-pool.io:3333` | 0 % | Código abierto, el que usan muchos Bitaxe. Por defecto. |
| solo.ckpool.org | `solo.ckpool.org:3333` (EE. UU.), `eusolo.ckpool.org:3333` (Europa), `ausolo.ckpool.org:3333` (Australia) | 2 % del bloque | El pool en solitario más veterano (desde 2014). Mejor con direcciones `bc1q…`. |

Cuidado: public-pool.io también tiene puertos de minería **compartida** (PPLNS,
p. ej. 13333). No los uses con este minero: ahí el bloque no te paga a ti. El
puerto antiguo 21496 ya no es el oficial desde junio de 2026.

Con cualquiera de los dos, el minero te enseña al conectar qué porcentaje de la
recompensa iría a tu dirección, así que lo puedes comprobar tú mismo. Si usas
un pool normal (no en solitario), el bloque paga al pool y el minero te avisará.

## Seguridad

- **Nunca escribas tu frase semilla (12 o 24 palabras) ni tu clave privada**
  en ningún programa, web o chat. El minero solo necesita tu dirección
  pública, y si pegas una frase semilla se niega a continuar.
- Desconfía de cualquier "minero" o "app de minería" que prometa ganancias con
  un PC o un móvil: casi siempre son estafas o malware.
- Este programa es Python legible, no descarga nada y solo se conecta al pool
  que elijas.

## Cómo funciona por dentro

- `minero_btc/stratum.py`: cliente Stratum v1 (suscripción, autorización,
  trabajos, envío de shares, reconexión).
- `minero_btc/consensus.py`: reglas de Bitcoin (doble SHA-256, objetivos y
  dificultad, raíz Merkle, cabecera de 80 bytes, lectura de la coinbase).
- `minero_btc/worker.py`: un proceso por hilo de CPU que recorre los 2³² nonces
  de cada cabecera, con la optimización *midstate* (los primeros 64 bytes de
  la cabecera se calculan una sola vez).
- `minero_btc/miner.py`: reparte el trabajo, envía las shares, comprueba a
  quién paga el bloque y muestra estadísticas.
- `minero_btc/address.py`: validación de direcciones (Base58Check, bech32 y
  bech32m) y detección de frases semilla y claves privadas.

### Tests

```
py -m unittest discover -s tests -v
```

Los tests incluyen el **bloque #1 real de Bitcoin** (el minero reconstruye su
cabecera a partir de un trabajo Stratum y encuentra su nonce auténtico), los
vectores oficiales de BIP173 y BIP350, y pruebas completas contra un pool
simulado en tu propio equipo: shares válidas, reconexión, bloque encontrado
(también cuando el pool pide shares más difíciles que la red), comprobación de
a quién paga el pool, un pool que te recorta el premio a mitad de sesión y un
pool hostil que envía mensajes malformados. No se conectan a Internet. Pasan
con Python 3.9, 3.10, 3.11, 3.12 y 3.13.
