# paper-bot-pumpfun

Bot de prueba para pump.fun con **dinero simulado**, en euros. No toca ninguna wallet ni pide claves: solo lee datos públicos y apunta lo que habría pasado, con comisiones y slippage incluidos.

- `aprende.py`: lo que aprende de cada operación cerrada y cómo decide con ello.
- `bot.py`: el bot. `python bot.py tick --dir docs` hace una pasada; `test_bot.py` lo prueba sin red.
- `.github/workflows/paper-bot.yml`: lo lanza cada 10 minutos; cada ejecución mira el mercado una vez por minuto y guarda el resultado en `docs/`.
- `docs/index.html`: informe (se publica con GitHub Pages). Incluye un apartado «Salud del bot» con sus propias comprobaciones.
- `docs/config.json` (opcional): para cambiar reglas sin tocar el código.
- `docs/estado.json`: la memoria del bot. Las pruebas cerradas hace más de 2 horas salen de ahí para que no crezca sin fin: lo aprendido de ellas y sus cifras se quedan resumidos dentro, y el detalle de cada una pasa a `docs/archivo/vN-AAAAMMDD-HH.csv` (hora UTC; N es el método de medición, ahora el 3). `docs/operaciones.csv` tiene solo las últimas. Las operaciones de la cuenta no se archivan.

Compara tres formas de entrar (control sin filtro, filtro básico, filtro de impulso) con cuatro formas de salir (x2 en 24 h, x2 en 1 h, +50 % en 1 h, +20 % en 15 min). El veredicto exige 50 operaciones cerradas por combinación. Un resultado positivo en simulación no garantiza ganar dinero.

Además lleva una **cuenta simulada de 30 €** con tres huecos: de entre las monedas que prueba, solo compra las que su aprendizaje ve con ganancia después de costes. Sin ejemplos no compra: de probar cosas se encarga el laboratorio, que no gasta saldo.

Cómo apunta las ventas: los precios salen de velas de 1 minuto, y dentro de un minuto no se sabe en qué orden pasó cada cosa, así que se apunta siempre lo peor. Si una moneda se hunde de golpe y ese minuto cierra por debajo del stop, se da por vendida al cierre (un stop no llega a tiempo en un desplome); si toca el objetivo pero cierra por debajo, también al cierre. Cada vez que cambia la forma de medir, el registro empieza de cero para no mezclar resultados.
