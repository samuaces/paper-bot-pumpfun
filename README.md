# paper-bot-pumpfun

Bot de prueba para pump.fun con **dinero simulado**. No toca ninguna wallet ni pide claves: solo lee datos públicos y apunta lo que habría pasado, con comisiones y slippage incluidos.

- `bot.py`: el bot. `python bot.py tick --dir docs` hace una pasada.
- `.github/workflows/paper-bot.yml`: lo ejecuta cada 10 minutos y guarda el resultado en `docs/`.
- `docs/index.html`: informe (se publica con GitHub Pages).
- `docs/config.json` (opcional): para cambiar reglas sin tocar el código.

Compara tres estrategias: control (sin filtro), filtro básico y filtro de impulso. El veredicto exige 50 operaciones cerradas por estrategia. Un resultado positivo en simulación no garantiza ganar dinero.
