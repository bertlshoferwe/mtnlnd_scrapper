"""
Provider-agnostic AI layer. scraper.py's AI-dependent functions
(ai_identify_job_links, ai_semantic_keyword_scan, generate_daily_summary)
call client.complete(prompt, max_tokens) without caring which provider is
behind it — this module is the only place that knows the difference between
Anthropic's and Gemini's SDKs/APIs.

Choose the provider with the AI_PROVIDER environment variable:
  AI_PROVIDER=anthropic   (default if unset and ANTHROPIC_API_KEY is present)
  AI_PROVIDER=gemini

Each provider needs its own API key:
  ANTHROPIC_API_KEY   from https://console.anthropic.com
  GEMINI_API_KEY      from https://aistudio.google.com — genuinely free tier,
                        no card required, but Google may use free-tier
                        prompts/responses to improve their products. Worth
                        weighing against that if scanned documents are
                        commercially sensitive.

If AI_PROVIDER isn't set, this falls back to whichever provider has a key
present (checked in the order: Anthropic, then Gemini), so existing setups
using just ANTHROPIC_API_KEY keep working unchanged.

Every provider's .complete() catches its own errors and returns None on any
failure rather than raising — callers already treat "no AI response" as a
normal, expected case (see scraper.py), so this keeps that behavior uniform
across both providers instead of every call site needing its own
provider-specific error handling.
"""

import os


class AIProvider:
    name = "unknown"

    def complete(self, prompt, max_tokens):
        """Return the model's text response, or None on any failure."""
        raise NotImplementedError


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self, api_key, model=None):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    def complete(self, prompt, max_tokens):
        try:
            response = self.client.messages.create(
                model=self.model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except Exception as e:
            print(f"  ! Anthropic API call failed: {e}")
            return None


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key, model=None):
        from google import genai
        self.client = genai.Client(api_key=api_key)
        # Google retires Gemini model names often (gemini-2.5-flash was pulled
        # for new projects in 2026). If this default stops working, set the
        # GEMINI_MODEL env var to the current one from https://ai.google.dev
        # — as of late 2026 that's the 3.x flash line (gemini-3.6-flash,
        # gemini-3.7-flash, …).
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

    def complete(self, prompt, max_tokens):
        try:
            from google.genai import types
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=max_tokens),
            )
            return response.text
        except Exception as e:
            print(f"  ! Gemini API call failed: {e}")
            return None


# ---------------------------------------------------------------------------
# Embeddings — used by scraper.py to pre-filter a large keyword list down to
# the terms actually relevant to a given document before the (expensive)
# semantic pass. Anthropic has no first-party embedding API, so this always
# routes through Gemini when a GEMINI_API_KEY is present, regardless of which
# provider AI_PROVIDER selects for chat. No key -> no embedder -> the scan
# falls back to a literal-hits-only pre-filter.
# ---------------------------------------------------------------------------

EMBED_DIM = 768


class GeminiEmbedder:
    def __init__(self, api_key, model=None):
        from google import genai
        self.client = genai.Client(api_key=api_key)
        self.model = model or os.environ.get("EMBED_MODEL", "gemini-embedding-001")

    BATCH = 100  # Gemini caps batchEmbedContents at 100 requests per call

    def embed(self, texts):
        """texts: list[str] -> list[list[float]] (len EMBED_DIM each), or None
        on any failure. Never raises. Sends in batches of 100."""
        texts = list(texts)
        if not texts:
            return []
        from google.genai import types
        out = []
        for i in range(0, len(texts), self.BATCH):
            chunk = texts[i:i + self.BATCH]
            try:
                resp = self.client.models.embed_content(
                    model=self.model,
                    contents=chunk,
                    config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
                )
                out.extend(list(e.values) for e in resp.embeddings)
            except Exception as e:
                print(f"  ! Embedding call failed (batch {i // self.BATCH + 1}): {e}")
                return None
        return out


def get_embedder():
    """A GeminiEmbedder if GEMINI_API_KEY is set, else None."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        return GeminiEmbedder(key)
    except Exception as e:
        print(f"! Failed to initialize embedder: {e}")
        return None


def get_provider():
    """
    Return an AIProvider instance based on AI_PROVIDER + the matching API
    key, or None if nothing is configured / initialization fails. Never
    raises — the caller (scraper.py) already treats a None client as
    "AI features are off" and runs the literal-matching-only path instead.
    """
    provider_name = (os.environ.get("AI_PROVIDER") or "").strip().lower()

    if not provider_name:
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider_name = "anthropic"
        elif os.environ.get("GEMINI_API_KEY"):
            provider_name = "gemini"
        else:
            return None

    try:
        if provider_name == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY")
            return AnthropicProvider(key) if key else None
        elif provider_name == "gemini":
            key = os.environ.get("GEMINI_API_KEY")
            return GeminiProvider(key) if key else None
        else:
            print(f"! Unknown AI_PROVIDER '{provider_name}' (expected anthropic/gemini) — AI features disabled")
            return None
    except Exception as e:
        print(f"! Failed to initialize AI provider '{provider_name}': {e}")
        return None
