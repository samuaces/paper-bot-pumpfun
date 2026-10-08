# paper-bot-pumpfun

Bot de prueba para pump.fun con **dinero simulado**. No toca ninguna wallet ni pide claves: solo lee datos públicos y apunta lo que habría pasado, con comisiones y slippage incluidos.

- `bot.py`: el bot. `python bot.py tick --dir docs` hace una pasada; `test_bot.py` lo prueba sin red.
- `.github/workflows/paper-bot.yml`: lo lanza cada 10 minutos; cada ejecución mira el mercado una vez por minuto y guarda el resultado en `docs/`.
- `docs/index.html`: informe (se publica con GitHub Pages). Incluye un apartado «Salud del bot» con sus propias comprobaciones.
- `docs/config.json` (opcional): para cambiar reglas sin tocar el código.

Compara tres formas de entrar (control sin filtro, filtro básico, filtro de impulso) con tres formas de salir (x2 en 24 h, x2 en 1 h, +50% en 1 h). El veredicto exige 50 operaciones cerradas por combinación. Un resultado positivo en simulación no garantiza ganar dinero.
