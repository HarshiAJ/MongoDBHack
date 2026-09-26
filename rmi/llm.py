"""Thin LLM wrapper so the provider and model are configured in one place (.env)."""

import os
from functools import lru_cache

from openai import OpenAI
from pydantic import BaseModel

import rmi.db  # noqa: F401  (loads .env)

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1")


@lru_cache
def client() -> OpenAI:
    return OpenAI()


def parse(system: str, user: str, schema: type[BaseModel], model: str = MODEL) -> BaseModel:
    """Structured output: the response is validated against `schema`."""
    resp = client().responses.parse(
        model=model,
        input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        text_format=schema,
        temperature=0,
    )
    return resp.output_parsed
