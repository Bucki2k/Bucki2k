# Minar Monero con tu Ryzen 7 9800X3D

Si lo que quieres es que tu PC **gane algo de verdad**, Monero es la mejor opción
para tu procesador. Su algoritmo (RandomX) está diseñado para CPUs y necesita
2 MB de caché L3 por hilo. Tu 9800X3D tiene 96 MB, así que sus 16 hilos
trabajan a pleno rendimiento.

## Cuánto puedes ganar

Datos de mediados de octubre de 2026: Monero cotiza a unos 530 $ y la red mina
a ~6,4 GH/s.

| | Valor aproximado |
|---|---|
| Velocidad de tu 9800X3D con XMRig | 12.000–13.500 H/s (la mejor prueba publicada es de 13.940 H/s) |
| Monero al día | ~0,0008–0,0009 XMR |
| En dólares | ~0,43–0,48 $/día, unos **13–15 $ al mes** |
| Consumo de la CPU | ~120 W |

- El precio de Monero y la potencia de la red cambian a diario, así que estas cifras se mueven.
- Con la luz gratis es ganancia neta. Pagando la luz a ~0,20 €/kWh, lo
  ganado no cubriría el consumo.
- Comparado con Bitcoin: minando Monero en solitario encontrarías de media un
  bloque cada ~1,8 años. Con Bitcoin serían miles de millones de años. Con un
  pool cobras poco a poco, con regularidad.

## Paso 1: una wallet de Monero

Instala una de estas wallets y **apunta la frase semilla en papel**, nunca en el PC:

- [Feather Wallet](https://featherwallet.org): ligera y fácil.
- [Monero GUI](https://www.getmonero.org/downloads/): la oficial.
- [Cake Wallet](https://cakewallet.com): para el móvil.

Tu dirección principal empieza por **4** y tiene 95 caracteres. Es la que
usarás para minar.

## Opción A (recomendada): Gupax, con pago directo a tu wallet y sin comisión

[Gupax](https://github.com/gupax-io/gupax) es un programa para Windows que junta
**P2Pool** y **XMRig**. P2Pool es un pool descentralizado sin dueño: cuando se
encuentra un bloque, tu parte va **directamente a tu wallet**, dentro del propio
bloque de Monero. No hay cuenta en ningún pool, no hay comisión y no hay mínimo
de cobro. Es justo lo que buscabas con Bitcoin, pero aquí sí cobrarás.

1. Descarga `gupax-v2.0.1-windows-x64.zip` de la
   [página oficial de releases](https://github.com/gupax-io/gupax/releases)
   (no de otra web).
2. Comprueba que el archivo no está manipulado. Abre una terminal en la carpeta
   de descargas y ejecuta:
   ```
   certutil -hashfile gupax-v2.0.1-windows-x64.zip SHA256
   ```
   El resultado debe ser
   `a025abce905eb59d41d88da50d3fda383cd1f05fc932941a8aacb3934118f8ce`.
3. Descomprímelo en una carpeta propia, por ejemplo `C:\Mineria\Gupax`. Los
   antivirus suelen marcar los mineros como "aplicación potencialmente no
   deseada", así que añade una exclusión en Windows Defender **solo para esa
   carpeta**. No desactives el antivirus.
4. Abre Gupax:
   - En la pestaña **P2Pool**, pega tu dirección (la que empieza por 4) y elige
     la cadena **Nano**, pensada para equipos de menos de 30 kH/s como el tuyo.
     Luego pulsa *Start*.
   - En la pestaña **XMRig**, pulsa *Start*. Gupax lo ejecuta como
     administrador para activar las *huge pages* y el *MSR mod*, que dan hasta
     un ~15 % más de velocidad.
5. Para el nodo de Monero hay dos opciones:
   - Usar un **nodo remoto**, que es lo más sencillo.
   - Montar **tu propio nodo** en la pestaña *Node*. Ocupa unos 105 GB y tú
     tienes ~320 GB libres. Es lo más privado y descentralizado.

En la cadena Nano, con tu velocidad, cobrarías de media aproximadamente cada
dos días, en pagos pequeños que llegan directos a tu wallet. La pestaña
*Status* de Gupax tiene una calculadora con los datos en vivo.

## Opción B (la más simple): XMRig y un solo pool

1. Descarga `xmrig-6.26.0-windows-x64.zip` de la
   [página oficial de XMRig](https://github.com/xmrig/xmrig/releases) y
   compruébalo:
   ```
   certutil -hashfile xmrig-6.26.0-windows-x64.zip SHA256
   ```
   El resultado debe ser
   `bba8097cb37d9b458a1cb1137876b27cde6740d17fe4ccbc086ba07d87d9e147`
   (es el valor del archivo `SHA256SUMS` oficial de esa versión).
2. Descomprímelo, por ejemplo en `C:\Mineria\xmrig`, y añade la exclusión del
   antivirus solo para esa carpeta.
3. Crea en esa carpeta un archivo `minar.bat` con esta línea, poniendo tu
   dirección:
   ```
   xmrig.exe -o pool.supportxmr.com:3333 -u TU_DIRECCION_XMR -p JasonPC -k
   ```
4. Haz clic derecho en `minar.bat` y elige **Ejecutar como administrador**. La
   primera vez XMRig activa las *huge pages* y pide reiniciar. Reinicia y
   vuelve a ejecutarlo como administrador.
5. En el registro deberías ver `huge pages 100%` y que los valores MSR se han
   aplicado correctamente.

Puedes ver tus estadísticas en <https://supportxmr.com> pegando tu dirección.

Pegas de esta opción frente a la A:

- SupportXMR cobra un 0,6 % y XMRig dona por defecto un 1 % a sus
  desarrolladores (opción `--donate-level`).
- El pool te paga cuando acumulas el mínimo, que por defecto es 0,3 XMR: casi
  un año a tu velocidad. Puedes bajarlo a 0,1 XMR (unos 4 meses) en la web del
  pool.
- La documentación de Monero recomienda no usar los pools más grandes, para no
  centralizar la red. P2Pool (opción A) no tiene ese problema.

## Mantenimiento

- Monero prepara una actualización de red (FCMP++, con RandomX v2) que aún no
  tiene fecha. Cuando se anuncie, actualiza XMRig, P2Pool y Gupax. XMRig 6.26.0
  ya es compatible.
- Minar mantiene la CPU al 100 %. El 9800X3D lo aguanta bien, pero vigila que
  la refrigeración esté limpia y que las temperaturas sean normales.
- Si quieres jugar, para el minero antes: compiten por la CPU.

## ¿Y la tarjeta gráfica (RX 9070 XT)?

No sirve para Monero, que se mina con CPU, pero puede minar otras monedas con
[SRBMiner-MULTI](https://github.com/doktor83/SRBMiner-Multi), que es compatible
con las RX 9000.

- Con monedas asentadas (Conflux, Quai, Ravencoin, Zano) son ~0,3–0,8 $ al día.
- Monedas nuevas como Pearl han llegado a 1–2 $ al día, pero son muy volátiles
  y el soporte para AMD ha aparecido y desaparecido varias veces en 2026.
- Se puede montar aparte si te interesa, pero minando con la GPU no puedes
  jugar a la vez.

## Seguridad

- Descarga solo de las páginas oficiales de GitHub y comprueba el SHA-256.
  Nunca uses versiones "reempaquetadas" ni mineros "silenciosos".
- Nunca compartas la frase semilla de tu wallet. Para minar solo hace falta tu
  dirección pública.

## Fuentes

- Pruebas de XMRig del 9800X3D: <https://xmrig.com/benchmark> (14 resultados
  publicados, de ~7.200 a 13.943 H/s; la mediana ronda los 12.000 H/s).
- Recompensa de Monero (0,6 XMR por bloque cada 2 minutos):
  <https://docs.getmonero.org>.
- XMRig 6.26.0 y sus sumas SHA-256: <https://github.com/xmrig/xmrig/releases/tag/v6.26.0>.
- Gupax v2.0.1: <https://github.com/gupax-io/gupax/releases/tag/v2.0.1>.
- P2Pool (pagos directos, 0 % de comisión): <https://github.com/SChernykh/p2pool>.
- SupportXMR (puertos, 0,6 % de comisión, mínimo de pago):
  <https://github.com/Snipa22/nodejs-pool> y la ayuda de <https://supportxmr.com>.
