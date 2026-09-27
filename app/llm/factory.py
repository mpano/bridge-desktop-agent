from app.config.settings import Settings
from app.llm.client import LLMClient, OpenAILLMClient
from app.llm.ollama import OllamaLLMClient


def create_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "ollama":
        return OllamaLLMClient(settings)
    if settings.llm_provider == "openai":
        return OpenAILLMClient(settings)
    raise ValueError("Unsupported LLM provider.")
