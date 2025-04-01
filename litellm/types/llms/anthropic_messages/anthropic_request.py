from typing import Any, Dict, List, Literal, Optional, TypedDict, Union

import httpx


class AnthropicMessagesRequestOptionalParams(TypedDict, total=False):
    """
    Anthropic Messages API Request Optional Params: https://docs.anthropic.com/en/api/messages
    """

    max_tokens: int
    metadata: Optional[Dict[str, Any]]
    stop_sequences: Optional[List[str]]
    stream: Literal[False]
    system: Optional[str]
    temperature: float
    thinking: Optional[Dict[str, Any]]
    tool_choice: Optional[Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]]
    top_k: Optional[int]
    top_p: Optional[float]
    pass


class AnthropicMessagesRequestParams(
    AnthropicMessagesRequestOptionalParams, total=False
):
    """
    Anthropic Messages API Request Params: https://docs.anthropic.com/en/api/messages
    """

    messages: List[Dict]
    model: str
