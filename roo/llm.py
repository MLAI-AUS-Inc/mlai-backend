"""
Model-Agnostic LLM Client for Roo

Supports multiple LLM providers with a unified interface.
Easy to swap models by changing configuration.
"""
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from enum import Enum


class LLMProvider(Enum):
    """Supported LLM providers."""
    GEMINI = "gemini"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


@dataclass
class LLMConfig:
    """Configuration for LLM client."""
    provider: LLMProvider
    model: str
    api_key: str
    base_url: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 2048


@dataclass
class LLMResponse:
    """Standardized response from LLM."""
    content: str
    model: str
    usage: Optional[Dict[str, int]] = None
    raw_response: Optional[Any] = None


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients."""
    
    def __init__(self, config: LLMConfig):
        self.config = config
    
    @abstractmethod
    def chat(self, messages: List[Dict[str, str]], **kwargs) -> LLMResponse:
        """Send a chat completion request."""
        raise NotImplementedError
    
    @abstractmethod
    def complete(self, prompt: str, **kwargs) -> LLMResponse:
        """Send a simple completion request."""
        raise NotImplementedError


class OpenAICompatibleClient(BaseLLMClient):
    """
    Client for OpenAI-compatible APIs.
    Works with: OpenAI, Gemini (via OpenAI-compatible endpoint), Azure OpenAI, etc.
    """
    
    def __init__(self, config: LLMConfig):
        super().__init__(config)
        from openai import OpenAI
        
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url
        )
    
    def chat(self, messages: List[Dict[str, str]], **kwargs) -> LLMResponse:
        """Send chat completion request."""
        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=kwargs.get('temperature', self.config.temperature),
            max_tokens=kwargs.get('max_tokens', self.config.max_tokens),
        )
        
        content = response.choices[0].message.content or ""
        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        
        return LLMResponse(
            content=content,
            model=response.model,
            usage=usage,
            raw_response=response
        )
    
    def complete(self, prompt: str, **kwargs) -> LLMResponse:
        """Send completion as a chat message."""
        messages = [{"role": "user", "content": prompt}]
        return self.chat(messages, **kwargs)


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic Claude API."""
    
    def __init__(self, config: LLMConfig):
        super().__init__(config)
        from anthropic import Anthropic
        
        self.client = Anthropic(api_key=config.api_key)
    
    def chat(self, messages: List[Dict[str, str]], **kwargs) -> LLMResponse:
        """Send chat completion request to Claude."""
        # Extract system message if present
        system = None
        chat_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                chat_messages.append(msg)
        
        response = self.client.messages.create(
            model=self.config.model,
            max_tokens=kwargs.get('max_tokens', self.config.max_tokens),
            system=system or "",
            messages=chat_messages,
        )
        
        content = response.content[0].text if response.content else ""
        usage = {
            "prompt_tokens": response.usage.input_tokens,
            "completion_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
        }
        
        return LLMResponse(
            content=content,
            model=response.model,
            usage=usage,
            raw_response=response
        )
    
    def complete(self, prompt: str, **kwargs) -> LLMResponse:
        """Send completion as a chat message."""
        messages = [{"role": "user", "content": prompt}]
        return self.chat(messages, **kwargs)


# Default configurations for common providers
DEFAULT_CONFIGS = {
    LLMProvider.GEMINI: {
        "model": "gemini-2.5-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_key": "GOOGLE_API_KEY",
    },
    LLMProvider.OPENAI: {
        "model": "gpt-4o-mini",
        "base_url": None,
        "env_key": "OPENAI_API_KEY",
    },
    LLMProvider.ANTHROPIC: {
        "model": "claude-3-5-sonnet-20241022",
        "base_url": None,
        "env_key": "ANTHROPIC_API_KEY",
    },
}


def get_llm_client(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    **kwargs
) -> BaseLLMClient:
    """
    Factory function to get an LLM client.
    
    Args:
        provider: Provider name ("gemini", "openai", "anthropic")
                  Defaults to GOOGLE_API_KEY if set, else OPENAI_API_KEY
        model: Model name (uses provider default if not specified)
        api_key: API key (reads from environment if not specified)
        **kwargs: Additional config options
    
    Returns:
        Configured LLM client
    
    Example:
        # Use default (Gemini if GOOGLE_API_KEY set)
        client = get_llm_client()
        
        # Use specific provider
        client = get_llm_client(provider="openai", model="gpt-4o")
        
        # Use custom API key
        client = get_llm_client(provider="gemini", api_key="...")
    """
    # Determine provider from environment if not specified
    if provider is None:
        if os.environ.get("GOOGLE_API_KEY"):
            provider = "gemini"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        else:
            raise ValueError(
                "No LLM API key found. Set GOOGLE_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY"
            )
    
    # Get provider enum
    try:
        provider_enum = LLMProvider(provider.lower())
    except ValueError:
        raise ValueError(f"Unknown provider: {provider}. Use 'gemini', 'openai', or 'anthropic'")
    
    # Get default config
    default = DEFAULT_CONFIGS[provider_enum]
    
    # Get API key from environment if not provided
    if api_key is None:
        api_key = os.environ.get(default["env_key"])
        if not api_key:
            raise ValueError(f"API key not found. Set {default['env_key']} environment variable")
    
    # Build config
    config = LLMConfig(
        provider=provider_enum,
        model=model or default["model"],
        api_key=api_key,
        base_url=kwargs.get("base_url", default["base_url"]),
        temperature=kwargs.get("temperature", 0.7),
        max_tokens=kwargs.get("max_tokens", 2048),
    )
    
    # Create client
    if provider_enum == LLMProvider.ANTHROPIC:
        return AnthropicClient(config)
    else:
        # OpenAI and Gemini both use OpenAI-compatible client
        return OpenAICompatibleClient(config)


# Convenience singleton for default client
_default_client: Optional[BaseLLMClient] = None


def get_default_client() -> BaseLLMClient:
    """Get or create the default LLM client."""
    global _default_client
    if _default_client is None:
        _default_client = get_llm_client()
    return _default_client


def chat(messages: List[Dict[str, str]], **kwargs) -> str:
    """
    Convenience function for quick chat completions.
    
    Example:
        response = chat([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello!"}
        ])
    """
    client = get_default_client()
    return client.chat(messages, **kwargs).content


def complete(prompt: str, **kwargs) -> str:
    """
    Convenience function for quick completions.
    
    Example:
        response = complete("What is 2+2?")
    """
    client = get_default_client()
    return client.complete(prompt, **kwargs).content
