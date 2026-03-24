import asyncio
import json
import os
import re
import time
import unicodedata
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from dotenv import load_dotenv
from playwright.async_api import async_playwright

#env
load_dotenv(Path(__file__).resolve().parent / ".env")

# ==============================================================================
# CONFIGURACIÓN DE IA
# ==============================================================================
AI_BACKEND = (os.getenv("AI_BACKEND") or "ollama").strip().lower()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
# Modelo para Ollama: qwen3.5 recomendado (más actualizado)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
_MANUAL_CANDIDATES = [
	Path(os.getenv("MANUAL_PDF_PATH") or ""),
	Path(__file__).resolve().parent / "manual" / "TeoricaAbreviada_LecturaFacil_2023-12_Interactivo.pdf",
	Path(__file__).resolve().parent.parent / "TeoricaAbreviada_LecturaFacil_2023-12_Interactivo.pdf",
]
MANUAL_PDF_PATH = next((p for p in _MANUAL_CANDIDATES if str(p) and p.exists()), _MANUAL_CANDIDATES[1])
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Otros
DELAY_ENTRE_PREGUNTAS = int(os.getenv("DELAY_ENTRE_PREGUNTAS", "0"))
DEBUG_IA = os.getenv("DEBUG_IA", "false").strip().lower() in ("1", "true", "yes")
TEST_URL = "https://www.todotest.com/tests/test.asp?tip=2&t=66"

# Selectores de cada página web / Selectors of each web page
SELECTOR_CONT_PREG = ".cont_preg"
SELECTOR_PREGUNTA = "p.preg"
SELECTOR_OPCIONES = "p.resp.hov"
SELECTOR_INDICE_ACTUAL = "a.idx_p.p_sel"
SELECTOR_INDICE_SIGUIENTE = "a.idx_p"
SELECTOR_B_CORREGIR = "#b_corregir"



WEB_CACHE: dict[str, list[dict[str, str]]] = {}
PAGE_CACHE: dict[str, str] = {}
PDF_CACHE: list[dict[str, str | int]] | None = None

STOPWORDS_WEB = {
	"a",
	"al",
	"con",
	"correcta",
	"correcto",
	"cuál",
	"cual",
	"de",
	"del",
	"el",
	"en",
	"es",
	"esta",
	"está",
	"este",
	"la",
	"las",
	"lo",
	"los",
	"más",
	"mas",
	"por",
	"puede",
	"qué",
	"que",
	"se",
	"siempre",
	"su",
	"un",
	"una",
	"vehículo",
	"vehiculos",
	"vehículo?",
	"vehiculo",
	"y",
}

def _normalizar(texto: str) -> str:
	return unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode("ascii")

def _tokens_opcion(texto: str) -> list[str]:
	limpio = _normalizar(texto)
	limpio = re.sub(r"^[abc]\)\s*", "", limpio)
	tokens = re.findall(r"\d+(?:[.,]\d+)?|[a-z]+", limpio)
	return [t for t in tokens if t not in STOPWORDS_WEB and (len(t) >= 3 or t in {"no", "si"})]

def _tokens_consulta(texto: str) -> list[str]:
	return _tokens_opcion(texto)

def _dominio_preferido(url: str) -> bool:
	host = urlparse(url).netloc.lower()
	return any(
		d in host
		for d in (
			"todotest.com",
			"practicatest.com",
			"mundotest.com",
			"circulaseguro.com",
			"dgt.es",
			"carglass.es",
			"autonocion.com",
		)
	)

def _limpiar_html(html: str) -> str:
	html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
	html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
	html = re.sub(r"(?is)<noscript.*?>.*?</noscript>", " ", html)
	html = re.sub(r"(?is)<[^>]+>", " ", html)
	html = unescape(html)
	html = re.sub(r"\s+", " ", html)
	return html.strip()

def _leer_pagina_web(url: str) -> str:
	if not url:
		return ""
	if url in PAGE_CACHE:
		return PAGE_CACHE[url]
	try:
		req = Request(
			url,
			headers={
				"User-Agent": "Mozilla/5.0",
				"Accept-Language": "es-ES,es;q=0.9",
			},
		)
		with urlopen(req, timeout=8) as resp:
			content_type = (resp.headers.get("Content-Type") or "").lower()
			if "text/html" not in content_type:
				PAGE_CACHE[url] = ""
				return ""
			raw = resp.read(250_000).decode("utf-8", errors="ignore")
		texto = _limpiar_html(raw)[:12000]
		PAGE_CACHE[url] = texto
		return texto
	except Exception:
		PAGE_CACHE[url] = ""
		return ""

def _cargar_pdf_chunks() -> list[dict[str, str | int]]:
	global PDF_CACHE
	if PDF_CACHE is not None:
		return PDF_CACHE
	PDF_CACHE = []
	if not MANUAL_PDF_PATH.exists():
		return PDF_CACHE
	try:
		from pypdf import PdfReader
		reader = PdfReader(str(MANUAL_PDF_PATH))
		for i, page in enumerate(reader.pages):
			texto = (page.extract_text() or "").strip()
			if not texto:
				continue
			texto = re.sub(r"\s+", " ", texto)
			if len(texto) < 80:
				continue
			PDF_CACHE.append({
				"page": i + 1,
				"text": texto[:5000],
				"norm": _normalizar(texto[:5000]),
			})
	except Exception:
		PDF_CACHE = []
	return PDF_CACHE

def _buscar_pdf(pregunta: str) -> str:
	chunks = _cargar_pdf_chunks()
	if not chunks:
		return ""
	tokens = _tokens_consulta(pregunta)
	if not tokens:
		return ""
	mejores: list[tuple[float, dict[str, str | int]]] = []
	for chunk in chunks:
		norm = str(chunk["norm"])
		score = 0.0
		for tok in tokens:
			if tok in norm:
				score += 2.0 if re.search(r"\d", tok) else 1.0
		if score <= 0:
			continue
		mejores.append((score, chunk))
	mejores.sort(key=lambda x: x[0], reverse=True)
	partes = []
	for score, chunk in mejores[:3]:
		partes.append(f"Pagina {chunk['page']} (score {score:.1f}): {str(chunk['text'])[:700]}")
	if partes:
		return "MANUAL DGT PDF:\n" + "\n---\n".join(partes)
	return ""

def _buscar_resultados_web(pregunta: str) -> list[dict[str, str]]:
	clave = _normalizar(pregunta)
	if clave in WEB_CACHE:
		return WEB_CACHE[clave]
	try:
		from ddgs import DDGS
		queries = [
			f"\"{pregunta}\"",
			f"\"{pregunta}\" \"respuesta correcta\"",
			f"\"{pregunta}\" site:todotest.com",
			f"\"{pregunta}\" site:practicatest.com",
			f"\"{pregunta}\" DGT test",
			f"\"{pregunta}\" Reglamento General de Circulacion",
		]
		resultados = []
		vistos = set()
		with DDGS() as ddgs:
			for q in queries:
				for r in ddgs.text(q, max_results=6):
					title = (r.get("title") or "").strip()
					body = (r.get("body") or r.get("snippet") or "").strip()
					href = (r.get("href") or "").strip()
					firma = (title[:120], body[:180], href)
					if body and firma not in vistos:
						vistos.add(firma)
						resultados.append({
							"title": title[:160],
							"body": body[:420],
							"href": href[:220],
						})
		WEB_CACHE[clave] = resultados
		resultados.sort(key=lambda r: (not _dominio_preferido(r.get("href", "")),))
		return resultados
	except Exception:
		return []

def _buscar_web(pregunta: str) -> str:
	"""Siempre busca en la web. Fuente principal para respuestas correctas."""
	resultados = _buscar_resultados_web(pregunta)
	if resultados:
		partes = []
		for r in resultados[:5]:
			texto = " | ".join(x for x in [r["title"], r["body"]] if x)
			if texto:
				partes.append(texto[:450])
		if partes:
			return "INFORMACION WEB ENCONTRADA (fuente principal):\n" + "\n---\n".join(partes)
	return ""

def _indice_explicito_en_texto(texto: str) -> int | None:
	txt = _normalizar(texto)
	patrones = [
		r"respuesta correcta\s*(?:es|:)?\s*la?\s*opcion\s*([abc012])",
		r"respuesta correcta\s*(?:es|:)?\s*([abc012])",
		r"la correcta\s*(?:es|:)?\s*la?\s*([abc012])",
		r"opcion correcta\s*(?:es|:)?\s*([abc012])",
		r"correcta\s*(?:es|:)?\s*la?\s*([abc012])",
	]
	for patron in patrones:
		m = re.search(patron, txt)
		if not m:
			continue
		valor = m.group(1)
		if valor in {"0", "1", "2"}:
			return int(valor)
		if valor in {"a", "b", "c"}:
			return ord(valor) - ord("a")
	return None

def _resolver_desde_web(pregunta: str, opciones: list[str]) -> int | None:
	resultados = _buscar_resultados_web(pregunta)
	if not resultados:
		return None
	mejor_idx = None
	mejor_score = 0.0
	for r in resultados[:6]:
		texto = " ".join(x for x in [r.get("title", ""), r.get("body", "")] if x)
		url = r.get("href", "")
		pagina = _leer_pagina_web(url)
		if pagina:
			texto = f"{texto} {pagina}"
		explicito = _indice_explicito_en_texto(texto)
		if explicito is not None and explicito < len(opciones):
			if DEBUG_IA:
				print(f"[DEBUG] Web explicita: {texto[:180]!r}")
			return explicito
		texto_norm = _normalizar(texto)
		for idx, opcion in enumerate(opciones):
			opcion_norm = _normalizar(opcion)
			tokens = _tokens_opcion(opcion)
			score = 0.0
			if opcion_norm and opcion_norm in texto_norm:
				score += 8.0
			for tok in tokens:
				if tok in texto_norm:
					score += 3.0 if re.search(r"\d", tok) else 1.0
			if tokens:
				score += min(1.5, sum(1 for tok in tokens if tok in texto_norm) / len(tokens))
			if pagina and opcion_norm and opcion_norm in _normalizar(pagina):
				score += 10.0
			if _dominio_preferido(url):
				score += 0.8
			if score > mejor_score:
				mejor_score = score
				mejor_idx = idx
	if mejor_idx is not None and mejor_score >= 6.0:
		if DEBUG_IA:
			print(f"[DEBUG] Web score: idx={mejor_idx} score={mejor_score:.2f}")
		return mejor_idx
	return None

def _resolver_desde_pdf(pregunta: str, opciones: list[str]) -> int | None:
	chunks = _cargar_pdf_chunks()
	if not chunks:
		return None
	tokens_pregunta = _tokens_consulta(pregunta)
	if not tokens_pregunta:
		return None
	mejor_idx = None
	mejor_score = 0.0
	for chunk in chunks:
		texto_norm = str(chunk["norm"])
		base_score = 0.0
		for tok in tokens_pregunta:
			if tok in texto_norm:
				base_score += 1.5 if re.search(r"\d", tok) else 0.6
		if base_score < 1.2:
			continue
		for idx, opcion in enumerate(opciones):
			score = base_score
			opcion_norm = _normalizar(opcion)
			tokens_op = _tokens_opcion(opcion)
			if opcion_norm and opcion_norm in texto_norm:
				score += 5.0
			for tok in tokens_op:
				if tok in texto_norm:
					score += 2.0 if re.search(r"\d", tok) else 0.9
			if score > mejor_score:
				mejor_score = score
				mejor_idx = idx
	if mejor_idx is not None and mejor_score >= 4.0:
		if DEBUG_IA:
			print(f"[DEBUG] PDF score: idx={mejor_idx} score={mejor_score:.2f}")
		return mejor_idx
	return None

def _construir_prompt(pregunta: str, opciones: list[str], contexto_web: str = "") -> str:
	base = """Eres un examinador implacable y experto en el Reglamento General de Circulacion (RGC) de Espana. Tu unico objetivo es acertar preguntas oficiales de la DGT sin cometer fallos.

Aplica estrictamente estas reglas:
1. Diferencia legalmente entre terminos como parar, estacionar y detenerse; calzada y arcen; masa maxima autorizada y tara. No son sinonimos.
2. Desconfia de opciones con absolutos como "siempre", "nunca", "en ningun caso" o "unicamente", salvo que la legislacion espanola de trafico lo establezca de forma expresa.
3. Ante la duda, la respuesta correcta suele ser la mas prudente, restrictiva y segura para la circulacion.
4. Analiza silenciosamente por que las otras opciones son incorrectas o trampas segun el RGC de Espana antes de decidir.
5. Usa como fuente principal el manual PDF y la normativa espanola de trafico. Si hay contexto web, usalo solo si coincide con la legislacion espanola vigente.
6. No inventes excepciones ni respondas por intuicion general: responde solo segun normativa espanola.

"""
	if contexto_web:
		base += contexto_web + "\n"
	else:
		base += "Normas DGT: carga detrás 10%/15%; lateral con panel; ventanillas no; moto no remolque noche; descarga no acera salvo señalizado; menores >135cm cinturón; airbag desactivar silla atrás; carga no ocultar luces.\n\n"
	base += "Pregunta: " + pregunta + "\n\nOpciones:\n"
	for i, opc in enumerate(opciones):
		base += f"{i}. {opc}\n"
	return base + "\nResponde UNICAMENTE con un solo numero: 0, 1 o 2. No escribas explicacion."

def _extraer_indice(texto: str, num_opciones: int) -> int:
	txt = (texto or "").strip().lower()
	m = re.search(r"\b(respuesta|es|opción|opcion)\s*[:\s]*([012])\b", txt)
	if m:
		return min(int(m.group(2)), num_opciones - 1)
	m = re.search(r"(?:la\s+)?([abc])\)", txt)
	if m:
		return min(ord(m.group(1)) - ord("a"), num_opciones - 1)
	nums = re.findall(r"\b[012]\b", txt)
	if nums:
		return min(int(nums[-1]), num_opciones - 1)
	return 0

def _resolver_ollama(pregunta: str, opciones: list[str]) -> int:
	try:
		import ollama
		ctx_web = _buscar_web(pregunta)
		ctx_pdf = _buscar_pdf(pregunta)
		ctx = "\n\n".join(x for x in [ctx_web, ctx_pdf] if x)
		prompt = _construir_prompt(pregunta, opciones, ctx)
		resp = ollama.chat(
			model=OLLAMA_MODEL,
			messages=[
				{"role": "system", "content": "Eres un experto en tests DGT de Espana. Usa el manual PDF y la normativa espanola vigente como fuente principal. Razona internamente y responde SOLO con 0, 1 o 2."},
				{"role": "user", "content": prompt},
			],
			options={"temperature": 0, "num_predict": 5},
		)
		raw = resp["message"]["content"]
		if DEBUG_IA:
			print(f"[DEBUG] Ollama: {raw!r}")
		return _extraer_indice(raw, len(opciones))
	except ImportError:
		print("Instala: pip install ollama")
		return 0
	except Exception as e:
		print(f"Error Ollama: {e}")
		return 0

def _resolver_groq(pregunta: str, opciones: list[str]) -> int:
	try:
		from groq import Groq
		client = Groq(api_key=GROQ_API_KEY)
		ctx = "\n\n".join(x for x in [_buscar_web(pregunta), _buscar_pdf(pregunta)] if x)
		prompt = _construir_prompt(pregunta, opciones, ctx)
		resp = client.chat.completions.create(
			model="llama-3.3-70b-versatile",
			messages=[{"role": "user", "content": prompt}],
			max_tokens=10,
			temperature=0,
		)
		texto = resp.choices[0].message.content or ""
		return _extraer_indice(texto, len(opciones))
	except ImportError:
		print("Instala: pip install groq")
		return 0
	except Exception as e:
		err_str = str(e)
		if "429" in err_str:
			match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.I)
			espera = int(float(match.group(1))) + 2 if match else 60
			print(f"Límite Groq. Espera {espera}s o usa AI_BACKEND=ollama")
		else:
			print(f"Error Groq: {e}")
		return 0

def _resolver_gemini(pregunta: str, opciones: list[str]) -> int:
	try:
		from google import genai
		client = genai.Client(api_key=GEMINI_API_KEY)
		ctx = "\n\n".join(x for x in [_buscar_web(pregunta), _buscar_pdf(pregunta)] if x)
		prompt = _construir_prompt(pregunta, opciones, ctx)
		config = None
		try:
			from google.genai import types
			config = types.GenerateContentConfig(
				tools=[types.Tool(google_search=types.GoogleSearch())],
			)
		except Exception:
			pass
		for intento in range(3):
			try:
				kwargs = {"model": "gemini-2.5-flash", "contents": prompt}
				if config:
					kwargs["config"] = config
				resp = client.models.generate_content(**kwargs)
				return _extraer_indice(resp.text.strip(), len(opciones))
			except Exception as e:
				err_str = str(e)
				if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
					match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.I)
					espera = int(float(match.group(1))) + 2 if match else 55
					if intento < 2:
						print(f"Límite Gemini. Esperando {espera}s...")
						time.sleep(espera)
					else:
						print("Cuota Gemini agotada. Usa AI_BACKEND=ollama")
						return 0
				else:
					print(f"Error Gemini: {e}")
					return 0
		return 0
	except ImportError:
		print("Instala: pip install google-genai")
		return 0

def _extraer_json(texto: str) -> dict[str, int]:
	texto = (texto or "").strip()
	if not texto:
		return {}
	match = re.search(r"\{.*\}", texto, re.S)
	if not match:
		return {}
	try:
		data = json.loads(match.group(0))
	except Exception:
		return {}
	resultado = {}
	for k, v in data.items():
		try:
			idx = int(v)
		except Exception:
			continue
		if idx in (0, 1, 2):
			resultado[str(k)] = idx
	return resultado

def _construir_prompt_lote(items: list[dict[str, object]]) -> str:
	base = """Eres un examinador implacable y experto en el Reglamento General de Circulacion (RGC) de Espana. Tu unico objetivo es acertar preguntas oficiales de la DGT sin cometer fallos.

Reglas:
1. Basa tus respuestas EXCLUSIVAMENTE en la legislacion espanola de trafico vigente.
2. Diferencia legalmente entre terminos similares: parar, estacionar, detenerse; calzada y arcen; MMA y tara; etc.
3. Desconfia de absolutos como "siempre", "nunca", "en ningun caso" o "unicamente", salvo que la norma lo diga de forma expresa.
4. Ante la duda, la correcta suele ser la opcion mas prudente, restrictiva y segura.
5. Si usas busqueda, prioriza normativa espanola, DGT y fuentes de tests de conducir espanoles.
6. No escribas explicaciones.

Devuelve SOLO un JSON valido. Sin markdown. Sin texto extra.
Formato exacto:
{"1": 0, "2": 2, "3": 1}

Preguntas:
"""
	for item in items:
		base += f'\n{item["id"]}. {item["pregunta"]}\n'
		for i, opcion in enumerate(item["opciones"]):
			base += f"{i}. {opcion}\n"
	return base

def _resolver_gemini_lote(items: list[dict[str, object]]) -> dict[str, int]:
	try:
		from google import genai
		client = genai.Client(api_key=GEMINI_API_KEY)
		prompt = _construir_prompt_lote(items)
		config = None
		try:
			from google.genai import types
			config = types.GenerateContentConfig(
				tools=[types.Tool(google_search=types.GoogleSearch())],
			)
		except Exception:
			pass
		for intento in range(3):
			try:
				kwargs = {"model": GEMINI_MODEL, "contents": prompt}
				if config:
					kwargs["config"] = config
				resp = client.models.generate_content(**kwargs)
				texto = resp.text.strip()
				if DEBUG_IA:
					print(f"[DEBUG] Gemini lote: {texto[:500]!r}")
				return _extraer_json(texto)
			except Exception as e:
				err_str = str(e)
				if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
					match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.I)
					espera = int(float(match.group(1))) + 2 if match else 55
					if intento < 2:
						print(f"Límite Gemini. Esperando {espera}s...")
						time.sleep(espera)
					else:
						print("Cuota Gemini agotada.")
						return {}
				else:
					print(f"Error Gemini lote: {e}")
					return {}
		return {}
	except ImportError:
		print("Instala: pip install google-genai")
		return {}

async def _recoger_test_actual(pagina) -> list[dict[str, object]]:
	items = []
	total_preguntas = await pagina.locator(SELECTOR_INDICE_SIGUIENTE).count()
	for idx in range(total_preguntas):
		cont = pagina.locator(SELECTOR_CONT_PREG).nth(idx)
		pregunta = await cont.locator(SELECTOR_PREGUNTA).inner_text()
		if ". " in pregunta:
			pregunta = pregunta.split(". ", 1)[-1]
		elementos = await cont.locator(SELECTOR_OPCIONES).all()
		opciones = [await elem.inner_text() for elem in elementos]
		items.append({
			"id": str(idx + 1),
			"pregunta": pregunta,
			"opciones": opciones,
		})
	return items

def resolver_pregunta(pregunta: str, opciones: list[str]) -> int:
	idx = _resolver_desde_web(pregunta, opciones)
	if idx is not None and idx < len(opciones):
		if DEBUG_IA:
			print("[DEBUG] Respuesta desde web")
		return idx
	idx = _resolver_desde_pdf(pregunta, opciones)
	if idx is not None and idx < len(opciones):
		if DEBUG_IA:
			print("[DEBUG] Respuesta desde PDF")
		return idx
	if AI_BACKEND == "ollama":
		return _resolver_ollama(pregunta, opciones)
	if AI_BACKEND == "groq" and GROQ_API_KEY:
		return _resolver_groq(pregunta, opciones)
	if AI_BACKEND == "gemini" and GEMINI_API_KEY:
		return _resolver_gemini(pregunta, opciones)
	if AI_BACKEND == "groq":
		print("GROQ_API_KEY no configurada. Usa .env")
		return 0
	if AI_BACKEND == "gemini":
		print("GEMINI_API_KEY no configurada. Usa .env")
		return 0
	return 0

async def main():
	print("Iniciando el bot controlador de tests...")
	print(f"IA: {AI_BACKEND}" + (f" (modelo: {OLLAMA_MODEL})" if AI_BACKEND == "ollama" else ""))
	if MANUAL_PDF_PATH.exists():
		print(f"Manual PDF: {MANUAL_PDF_PATH}")
	else:
		print("Manual PDF no encontrado. Se usara web/IA.")
	url = (input(f"Introduce la URL del test [{TEST_URL}]: ").strip() or TEST_URL)

	async with async_playwright() as p:
		navegador = await p.chromium.launch(headless=False)
		contexto = await navegador.new_context()
		pagina = await contexto.new_page()

		print(f"Navegando a {url}...")
		await pagina.goto(url)

		print("Tómate 15 segundos para iniciar sesión si es necesario...")
		await pagina.wait_for_timeout(15000)

		if AI_BACKEND == "gemini" and GEMINI_API_KEY:
			try:
				print("\n--- Recogiendo test completo para Gemini ---")
				items = await _recoger_test_actual(pagina)
				print(f"Preguntas encontradas: {len(items)}")
				respuestas = _resolver_gemini_lote(items)
				print(f"Respuestas recibidas: {len(respuestas)}")
				for item in items:
					qid = str(item["id"])
					opciones = item["opciones"]
					if not opciones:
						continue
					indice_respuesta = respuestas.get(qid, 0)
					print(f"Pregunta {qid}: respuesta {indice_respuesta}")
					siguiente_id = f'[id="{qid}_idx_p"]'
					await pagina.locator(siguiente_id).first.evaluate("el => el.click()")
					await pagina.wait_for_timeout(300)
					cont = pagina.locator(SELECTOR_CONT_PREG).nth(int(qid) - 1)
					elementos = await cont.locator(SELECTOR_OPCIONES).all()
					if 0 <= indice_respuesta < len(elementos):
						await elementos[indice_respuesta].click()
						await pagina.wait_for_timeout(300)
				print("Test completado.")
				try:
					await pagina.locator(SELECTOR_B_CORREGIR).click(timeout=3000)
					print("Corrección enviada.")
				except Exception:
					pass
				await pagina.wait_for_timeout(1200)
				await navegador.close()
				return
			except Exception as e:
				print(f"Error en modo Gemini por lote: {e}")

		while True:
			try:
				print("\n--- Buscando nueva pregunta ---")
				await pagina.wait_for_selector(SELECTOR_INDICE_ACTUAL, state="attached", timeout=5000)

				indice_actual = await pagina.locator(SELECTOR_INDICE_ACTUAL).first.get_attribute("id")
				if not indice_actual:
					print("No se encontró índice actual.")
					break
				num_pregunta = int(indice_actual.replace("_idx_p", ""))
				cont_visible = pagina.locator(SELECTOR_CONT_PREG).nth(num_pregunta - 1)

				ya_seleccionada = (
					await cont_visible.locator("p.resp.sel").count() > 0
					or await cont_visible.locator("p.resp.selected").count() > 0
					or await cont_visible.locator("p.resp.active").count() > 0
				)
				if ya_seleccionada:
					print(f"Pregunta {num_pregunta} ya respondida, saltando...")
				else:
					texto_pregunta = await cont_visible.locator(SELECTOR_PREGUNTA).inner_text()
					if ". " in texto_pregunta:
						texto_pregunta = texto_pregunta.split(". ", 1)[-1]
					print(f"Pregunta {num_pregunta}: {texto_pregunta}")

					elementos_opciones = await cont_visible.locator(SELECTOR_OPCIONES).all()
					textos_opciones = [await elem.inner_text() for elem in elementos_opciones]
					print(f"Opciones encontradas: {textos_opciones}")

					if not textos_opciones:
						print("No se encontraron opciones.")
						break

					if DELAY_ENTRE_PREGUNTAS > 0:
						await pagina.wait_for_timeout(DELAY_ENTRE_PREGUNTAS * 1000)

					indice_respuesta = resolver_pregunta(texto_pregunta, textos_opciones)
					print(f"Respuesta elegida: {indice_respuesta} -> {textos_opciones[indice_respuesta]}")

					if 0 <= indice_respuesta < len(elementos_opciones):
						await elementos_opciones[indice_respuesta].click()
						print("¡Clic realizado!")
					await pagina.wait_for_timeout(600)

				total_preguntas = await pagina.locator(SELECTOR_INDICE_SIGUIENTE).count()
				if num_pregunta < total_preguntas:
					siguiente_id = f'[id="{(num_pregunta + 1)}_idx_p"]'
					await pagina.locator(siguiente_id).first.evaluate("el => el.click()")
					print(f"Pasando a pregunta {num_pregunta + 1}...")
				else:
					print("Test completado.")
					try:
						await pagina.locator(SELECTOR_B_CORREGIR).click(timeout=3000)
						print("Corrección enviada.")
					except Exception:
						pass
					break

				await pagina.wait_for_timeout(600)

			except Exception as e:
				if "TargetClosedError" in type(e).__name__ or "Target page" in str(e):
					print("Navegador cerrado.")
					break
				print(f"Error: {e}")
				try:
					await pagina.wait_for_timeout(2000)
				except Exception:
					break

		try:
			await navegador.close()
		except Exception:
			pass

if __name__ == "__main__":
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print("\nDetenido por el usuario.")
