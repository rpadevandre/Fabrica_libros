"""Cliente Claude unificado.

- Retry exponencial ante errores transitorios.
- Extraccion robusta de JSON (los modelos a veces envuelven en fences).
- Registro de costo por llamada via CostTracker inyectado.
"""
from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from typing import Any, Optional, Type, TypeVar

from anthropic import AsyncAnthropic, APIStatusError, APITimeoutError
from pydantic import BaseModel, ValidationError

from bookforge.core.config import settings

T = TypeVar("T", bound=BaseModel)

# Precios por millon de tokens (input, output). Actualizar si cambian.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-4-8": (Decimal("15"), Decimal("75")),
    "claude-sonnet-4-6": (Decimal("3"), Decimal("15")),
}


class LLMError(Exception):
    pass


def extract_json(text: str) -> Any:
    """Extrae el primer objeto/array JSON valido de una respuesta."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Busqueda del primer bloque balanceado { } o [ ]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"No se pudo extraer JSON de la respuesta: {text[:200]}")


class LLMClient:
    def __init__(self, cost_tracker: Optional["CostTrackerProtocol"] = None):
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._cost_tracker = cost_tracker

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        book_id: str | None = None,
        phase: str = "unknown",
    ) -> str:
        model = model or settings.model_volume
        last_err: Exception | None = None
        for attempt in range(settings.max_retries):
            try:
                resp = await self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system or None,
                    messages=[{"role": "user", "content": prompt}],
                )
                self._record_cost(model, resp.usage, book_id, phase)
                return "".join(
                    b.text for b in resp.content if b.type == "text"
                )
            except (APITimeoutError, APIStatusError) as exc:
                last_err = exc
                status = getattr(exc, "status_code", None)
                if status is not None and status < 500 and status != 429:
                    raise LLMError(f"Error no recuperable ({status}): {exc}")
                await asyncio.sleep(2 ** attempt)
        raise LLMError(f"Agotados los reintentos: {last_err}")

    async def complete_structured(
        self,
        prompt: str,
        schema: Type[T],
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.4,
        book_id: str | None = None,
        phase: str = "unknown",
    ) -> T:
        """Completa y valida contra un modelo Pydantic. Reintenta una vez
        con el error de validacion en el prompt si falla."""
        json_schema = json.dumps(schema.model_json_schema(), indent=None)
        full_system = (
            (system + "\n\n" if system else "")
            + "Responde UNICAMENTE con un objeto JSON valido que cumpla este "
              "schema. Sin texto adicional, sin markdown fences.\n"
            + json_schema
        )
        raw = await self.complete(
            prompt, system=full_system, model=model, max_tokens=max_tokens,
            temperature=temperature, book_id=book_id, phase=phase,
        )
        try:
            return schema.model_validate(extract_json(raw))
        except (ValidationError, LLMError) as first_err:
            repair = (
                f"{prompt}\n\nTu respuesta anterior fallo la validacion:\n"
                f"{first_err}\n\nCorrige y responde solo el JSON."
            )
            raw = await self.complete(
                repair, system=full_system, model=model,
                max_tokens=max_tokens, temperature=0.2,
                book_id=book_id, phase=phase,
            )
            return schema.model_validate(extract_json(raw))

    def _record_cost(self, model: str, usage: Any, book_id: str | None,
                     phase: str) -> None:
        if self._cost_tracker is None or usage is None:
            return
        in_price, out_price = PRICING.get(model, (Decimal("3"), Decimal("15")))
        cost = (
            Decimal(usage.input_tokens) * in_price
            + Decimal(usage.output_tokens) * out_price
        ) / Decimal("1000000")
        self._cost_tracker.record(book_id=book_id, phase=phase, model=model,
                                  cost_usd=cost,
                                  input_tokens=usage.input_tokens,
                                  output_tokens=usage.output_tokens)


class CostTrackerProtocol:
    def record(self, *, book_id: str | None, phase: str, model: str,
               cost_usd: Decimal, input_tokens: int,
               output_tokens: int) -> None: ...
