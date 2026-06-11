"""Pipeline 3 — Identidad visual.

- CoverArtDirector: genera arte base con gpt-image-2 (sin texto).
- CoverCompositor: compone titulo/subtitulo/autor con Pillow, porque los
  modelos de imagen no renderizan tipografia fiable.
- Test de legibilidad en thumbnail (100px) automatico.
- BlurbWriter: descripcion de Amazon con HTML permitido.
"""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

from bookforge.core.config import settings
from bookforge.core.llm import LLMClient
from bookforge.core.models import BrandKit, COVER_SPECS, MarketBrief


class ImageGenError(Exception):
    pass


async def generate_cover_art(prompt: str, width: int, height: int) -> bytes:
    """Llama a la API de imagenes de OpenAI (gpt-image-2). Devuelve PNG."""
    if not settings.openai_api_key:
        raise ImageGenError("BF_OPENAI_API_KEY no configurada")
    # gpt-image acepta tamaños fijos; pedimos el mas cercano vertical
    size = "1024x1536" if height > width else "1024x1024"
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={"model": settings.image_model, "prompt": prompt,
                  "size": size, "n": 1},
        )
        if resp.status_code != 200:
            raise ImageGenError(f"imagegen {resp.status_code}: {resp.text[:300]}")
        data = resp.json()["data"][0]
        if "b64_json" in data:
            return base64.b64decode(data["b64_json"])
        async with httpx.AsyncClient(timeout=60) as dl:
            img = await dl.get(data["url"])
            return img.content


class CoverPipeline:
    def __init__(self, llm: LLMClient, output_dir: Path | None = None):
        self.llm = llm
        self.output_dir = output_dir or settings.data_dir / "covers"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def art_prompt(self, brief: MarketBrief, brand: BrandKit,
                         book_id: str | None = None) -> str:
        prompt = (
            "Escribe UN prompt de generacion de imagen para la portada de "
            "este libro. Reglas: sin texto ni letras en la imagen, "
            "composicion con tercio superior y centro despejados para "
            "tipografia, debe cumplir convenciones visuales del genero y a "
            "la vez destacar en un grid de Amazon.\n"
            f"Estilo de marca (obligatorio respetar): {brand.cover_style_prompt}\n"
            f"Paleta: {brand.palette}\n"
            f"Libro: {brief.working_title} | nicho: {brief.niche} | "
            f"persona: {brief.target_persona.model_dump_json()}\n"
            "Responde solo el prompt, en ingles."
        )
        return (await self.llm.complete(
            prompt, model=settings.model_volume, max_tokens=500,
            temperature=0.7, book_id=book_id, phase="p3.art_director",
        )).strip()

    @staticmethod
    def _load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
        candidates = [
            Path(f"/usr/share/fonts/truetype/dejavu/{name}.ttf"),
            Path(f"templates/fonts/{name}.ttf"),
        ]
        for c in candidates:
            if c.exists():
                return ImageFont.truetype(str(c), size)
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size
        )

    def compose(self, art_png: bytes, title: str, subtitle: str,
                author: str, brand: BrandKit,
                channel: str = "kdp_ebook") -> Image.Image:
        spec = COVER_SPECS[channel]
        img = Image.open(BytesIO(art_png)).convert("RGB")
        img = img.resize((spec.width_px, spec.height_px), Image.LANCZOS)
        draw = ImageDraw.Draw(img, "RGBA")
        w, h = img.size
        accent = brand.palette[0] if brand.palette else "#FFFFFF"

        # Banda semitransparente para legibilidad
        draw.rectangle([0, int(h * 0.05), w, int(h * 0.30)],
                       fill=(0, 0, 0, 140))

        title_font = self._load_font(brand.title_font, int(w * 0.085))
        sub_font = self._load_font(brand.body_font, int(w * 0.04))
        author_font = self._load_font(brand.body_font, int(w * 0.045))

        self._draw_centered(draw, title.upper(), title_font, w,
                            int(h * 0.08), fill="#FFFFFF",
                            max_width=int(w * 0.9))
        self._draw_centered(draw, subtitle, sub_font, w, int(h * 0.225),
                            fill=accent, max_width=int(w * 0.85))
        self._draw_centered(draw, author, author_font, w, int(h * 0.92),
                            fill="#FFFFFF", max_width=int(w * 0.8))
        return img

    @staticmethod
    def _draw_centered(draw: ImageDraw.ImageDraw, text: str,
                       font: ImageFont.FreeTypeFont, canvas_w: int, y: int,
                       fill: str, max_width: int) -> None:
        words, lines, current = text.split(), [], ""
        for word in words:
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        line_h = int(font.size * 1.25)
        for i, line in enumerate(lines):
            tw = draw.textlength(line, font=font)
            draw.text(((canvas_w - tw) / 2, y + i * line_h), line,
                      font=font, fill=fill)

    @staticmethod
    def thumbnail_legibility_check(cover: Image.Image) -> dict:
        """Heuristica: contraste del area del titulo en thumbnail de 100px."""
        thumb = cover.copy()
        thumb.thumbnail((100, 160))
        gray = thumb.convert("L")
        top = gray.crop((0, int(gray.height * 0.05),
                         gray.width, int(gray.height * 0.30)))
        pixels = list(top.getdata())
        contrast = (max(pixels) - min(pixels)) if pixels else 0
        return {"thumbnail_contrast": contrast, "passes": contrast >= 90}

    async def run(self, brief: MarketBrief, brand: BrandKit, author: str,
                  book_id: str | None = None) -> dict:
        prompt = await self.art_prompt(brief, brand, book_id)
        art = await generate_cover_art(prompt, 1024, 1536)
        results = {}
        for channel in COVER_SPECS:
            cover = self.compose(art, brief.working_title, "", author,
                                 brand, channel)
            path = self.output_dir / f"{book_id or 'book'}_{channel}.jpg"
            cover.save(path, "JPEG", quality=92)
            results[channel] = {
                "path": str(path),
                **self.thumbnail_legibility_check(cover),
            }
        return {"art_prompt": prompt, "covers": results}


class BlurbWriter:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def write(self, brief: MarketBrief, book_id: str | None = None) -> str:
        prompt = (
            "Escribe la descripcion de Amazon (blurb) para este libro, en "
            "ingles. Reglas:\n"
            "- Las 2 primeras lineas son el hook (es lo visible antes de "
            "'Read more'): dolor + promesa.\n"
            "- HTML permitido por KDP: <b>, <i>, <br>, <ul><li>.\n"
            "- Bullets de beneficios concretos (no features).\n"
            "- Cierra con CTA suave.\n"
            "- 150-220 palabras. Sin frases-cliche de IA.\n\n"
            f"BRIEF: {brief.model_dump_json()}"
        )
        return (await self.llm.complete(
            prompt, model=settings.model_volume, max_tokens=1200,
            temperature=0.7, book_id=book_id, phase="p3.blurb",
        )).strip()
