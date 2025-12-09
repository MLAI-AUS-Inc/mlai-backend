"""
Base Tool Interface for Roo Agent

All tools inherit from BaseTool and implement the execute method.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ToolResult:
    """Result from a tool execution."""
    success: bool
    data: Any
    message: str
    error: Optional[str] = None


class BaseTool(ABC):
    """
    Base class for all Roo agent tools.
    
    Each tool must define:
    - name: Unique identifier for the tool
    - description: Human-readable description (used by LLM for selection)
    - execute: The main execution method
    """
    
    name: str = "base_tool"
    description: str = "Base tool - do not use directly"
    
    @abstractmethod
    def execute(self, query: str, user_id: str, **kwargs) -> ToolResult:
        """
        Execute the tool with the given query.
        
        Args:
            query: The user's message/request
            user_id: Slack user ID of the requester
            **kwargs: Additional context (channel_id, thread_ts, etc.)
        
        Returns:
            ToolResult with success status and data/message
        """
        raise NotImplementedError
    
    def get_tool_spec(self) -> dict:
        """Return tool specification for LLM function calling."""
        return {
            "name": self.name,
            "description": self.description,
        }
