# Bot de Autoescuela 

Bot que automatiza tests de autoescuela en TodoTest.

## Opciones de IA (sin límites / cuota generosa)

| Backend | Límites | Configuración |
|---------|---------|---------------|
| **Ollama** | Sin límites (local) | `AI_BACKEND=ollama` + instalar Ollama |
| **Groq** | ~30 req/min gratis | `AI_BACKEND=groq` + `GROQ_API_KEY` |
| **Gemini** | Cuota diaria | `AI_BACKEND=gemini` + `GEMINI_API_KEY` |

### Ollama (recomendado - sin límites)
1. Instala [Ollama](https://ollama.com/download)
2. Descarga un modelo: `ollama pull llama3.2`
3. En `.env`: `AI_BACKEND=ollama` (por defecto)

### Groq (gratis, cuota generosa)
1. API key en https://console.groq.com
2. En `.env`: `AI_BACKEND=groq` y `GROQ_API_KEY=tu_clave`

### Gemini
1. API key en https://aistudio.google.com
2. En `.env`: `AI_BACKEND=gemini` y `GEMINI_API_KEY=tu_clave`

## Uso
```bash
pip install -r requirements.txt
playwright install
python main.py
```
