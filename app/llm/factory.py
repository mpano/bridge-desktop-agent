from app.config.settings import Settings
from app.llm.client import LLMClient, OpenAILLMClient


def create_llm(settings: Settings) -> LLMClient:
    return OpenAILLMClient(settings)
