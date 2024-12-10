import json
import time
import types
from re import A
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
    cast,
)

import httpx

import litellm
from litellm.litellm_core_utils.core_helpers import map_finish_reason
from litellm.llms.base_llm.base_model_iterator import BaseModelResponseIterator
from litellm.llms.base_llm.transformation import BaseConfig, BaseLLMException
from litellm.llms.prompt_templates.factory import anthropic_messages_pt
from litellm.types.llms.anthropic import (
    AllAnthropicToolsValues,
    AnthropicChatCompletionUsageBlock,
    AnthropicComputerTool,
    AnthropicHostedTools,
    AnthropicInputSchema,
    AnthropicMessagesTool,
    AnthropicMessagesToolChoice,
    AnthropicSystemMessageContent,
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageBlockDelta,
    MessageStartBlock,
    UsageDelta,
)
from litellm.types.llms.openai import (
    AllMessageValues,
    ChatCompletionCachedContent,
    ChatCompletionSystemMessage,
    ChatCompletionToolCallChunk,
    ChatCompletionToolCallFunctionChunk,
    ChatCompletionToolParam,
    ChatCompletionToolParamFunctionChunk,
    ChatCompletionUsageBlock,
)
from litellm.types.utils import GenericStreamingChunk
from litellm.types.utils import Message as LitellmMessage
from litellm.types.utils import PromptTokensDetailsWrapper
from litellm.utils import ModelResponse, Usage, add_dummy_tool, has_tool_call_blocks

from ..common_utils import AnthropicError, process_anthropic_headers

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as _LiteLLMLoggingObj

    LiteLLMLoggingObj = _LiteLLMLoggingObj
else:
    LiteLLMLoggingObj = Any


class AnthropicConfig(BaseConfig):
    """
    Reference: https://docs.anthropic.com/claude/reference/messages_post

    to pass metadata to anthropic, it's {"user_id": "any-relevant-information"}
    """

    max_tokens: Optional[int] = (
        4096  # anthropic requires a default value (Opus, Sonnet, and Haiku have the same default)
    )
    stop_sequences: Optional[list] = None
    temperature: Optional[int] = None
    top_p: Optional[int] = None
    top_k: Optional[int] = None
    metadata: Optional[dict] = None
    system: Optional[str] = None

    def __init__(
        self,
        max_tokens: Optional[
            int
        ] = 4096,  # You can pass in a value yourself or use the default value 4096
        stop_sequences: Optional[list] = None,
        temperature: Optional[int] = None,
        top_p: Optional[int] = None,
        top_k: Optional[int] = None,
        metadata: Optional[dict] = None,
        system: Optional[str] = None,
    ) -> None:
        locals_ = locals()
        for key, value in locals_.items():
            if key != "self" and value is not None:
                setattr(self.__class__, key, value)

    @classmethod
    def get_config(cls):
        return super().get_config()

    def get_supported_openai_params(self, model: str):
        return [
            "stream",
            "stop",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "tools",
            "tool_choice",
            "extra_headers",
            "parallel_tool_calls",
            "response_format",
            "user",
        ]

    def get_cache_control_headers(self) -> dict:
        return {
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
        }

    def get_anthropic_headers(
        self,
        api_key: str,
        anthropic_version: Optional[str] = None,
        computer_tool_used: bool = False,
        prompt_caching_set: bool = False,
        pdf_used: bool = False,
        is_vertex_request: bool = False,
    ) -> dict:
        import json

        betas = []
        if prompt_caching_set:
            betas.append("prompt-caching-2024-07-31")
        if computer_tool_used:
            betas.append("computer-use-2024-10-22")
        if pdf_used:
            betas.append("pdfs-2024-09-25")
        headers = {
            "anthropic-version": anthropic_version or "2023-06-01",
            "x-api-key": api_key,
            "accept": "application/json",
            "content-type": "application/json",
        }

        # Don't send any beta headers to Vertex, Vertex has failed requests when they are sent
        if is_vertex_request is True:
            pass
        elif len(betas) > 0:
            headers["anthropic-beta"] = ",".join(betas)

        return headers

    def _map_tool_choice(
        self, tool_choice: Optional[str], parallel_tool_use: Optional[bool]
    ) -> Optional[AnthropicMessagesToolChoice]:
        _tool_choice: Optional[AnthropicMessagesToolChoice] = None
        if tool_choice == "auto":
            _tool_choice = AnthropicMessagesToolChoice(
                type="auto",
            )
        elif tool_choice == "required":
            _tool_choice = AnthropicMessagesToolChoice(type="any")
        elif isinstance(tool_choice, dict):
            _tool_name = tool_choice.get("function", {}).get("name")
            _tool_choice = AnthropicMessagesToolChoice(type="tool")
            if _tool_name is not None:
                _tool_choice["name"] = _tool_name

        if parallel_tool_use is not None:
            # Anthropic uses 'disable_parallel_tool_use' flag to determine if parallel tool use is allowed
            # this is the inverse of the openai flag.
            if _tool_choice is not None:
                _tool_choice["disable_parallel_tool_use"] = not parallel_tool_use
            else:  # use anthropic defaults and make sure to send the disable_parallel_tool_use flag
                _tool_choice = AnthropicMessagesToolChoice(
                    type="auto",
                    disable_parallel_tool_use=not parallel_tool_use,
                )
        return _tool_choice

    def _map_tool_helper(
        self, tool: ChatCompletionToolParam
    ) -> AllAnthropicToolsValues:
        returned_tool: Optional[AllAnthropicToolsValues] = None

        if tool["type"] == "function" or tool["type"] == "custom":
            _input_schema: dict = tool["function"].get(
                "parameters",
                {
                    "type": "object",
                    "properties": {},
                },
            )
            input_schema: AnthropicInputSchema = AnthropicInputSchema(**_input_schema)
            _tool = AnthropicMessagesTool(
                name=tool["function"]["name"],
                input_schema=input_schema,
            )

            _description = tool["function"].get("description")
            if _description is not None:
                _tool["description"] = _description

            returned_tool = _tool

        elif tool["type"].startswith("computer_"):
            ## check if all required 'display_' params are given
            if "parameters" not in tool["function"]:
                raise ValueError("Missing required parameter: parameters")

            _display_width_px: Optional[int] = tool["function"]["parameters"].get(
                "display_width_px"
            )
            _display_height_px: Optional[int] = tool["function"]["parameters"].get(
                "display_height_px"
            )
            if _display_width_px is None or _display_height_px is None:
                raise ValueError(
                    "Missing required parameter: display_width_px or display_height_px"
                )

            _computer_tool = AnthropicComputerTool(
                type=tool["type"],
                name=tool["function"].get("name", "computer"),
                display_width_px=_display_width_px,
                display_height_px=_display_height_px,
            )

            _display_number = tool["function"]["parameters"].get("display_number")
            if _display_number is not None:
                _computer_tool["display_number"] = _display_number

            returned_tool = _computer_tool
        elif tool["type"].startswith("bash_") or tool["type"].startswith(
            "text_editor_"
        ):
            function_name = tool["function"].get("name")
            if function_name is None:
                raise ValueError("Missing required parameter: name")

            returned_tool = AnthropicHostedTools(
                type=tool["type"],
                name=function_name,
            )
        if returned_tool is None:
            raise ValueError(f"Unsupported tool type: {tool['type']}")

        ## check if cache_control is set in the tool
        _cache_control = tool.get("cache_control", None)
        _cache_control_function = tool.get("function", {}).get("cache_control", None)
        if _cache_control is not None:
            returned_tool["cache_control"] = _cache_control
        elif _cache_control_function is not None and isinstance(
            _cache_control_function, dict
        ):
            returned_tool["cache_control"] = ChatCompletionCachedContent(
                **_cache_control_function  # type: ignore
            )

        return returned_tool

    def _map_tools(self, tools: List) -> List[AllAnthropicToolsValues]:
        anthropic_tools = []
        for tool in tools:
            if "input_schema" in tool:  # assume in anthropic format
                anthropic_tools.append(tool)
            else:  # assume openai tool call
                new_tool = self._map_tool_helper(tool)

                anthropic_tools.append(new_tool)
        return anthropic_tools

    def _map_stop_sequences(
        self, stop: Optional[Union[str, List[str]]]
    ) -> Optional[List[str]]:
        new_stop: Optional[List[str]] = None
        if isinstance(stop, str):
            if (
                stop == "\n"
            ) and litellm.drop_params is True:  # anthropic doesn't allow whitespace characters as stop-sequences
                return new_stop
            new_stop = [stop]
        elif isinstance(stop, list):
            new_v = []
            for v in stop:
                if (
                    v == "\n"
                ) and litellm.drop_params is True:  # anthropic doesn't allow whitespace characters as stop-sequences
                    continue
                new_v.append(v)
            if len(new_v) > 0:
                new_stop = new_v
        return new_stop

    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
    ) -> dict:
        for param, value in non_default_params.items():
            if param == "max_tokens":
                optional_params["max_tokens"] = value
            if param == "max_completion_tokens":
                optional_params["max_tokens"] = value
            if param == "tools":
                optional_params["tools"] = self._map_tools(value)
            if param == "tool_choice" or param == "parallel_tool_calls":
                _tool_choice: Optional[AnthropicMessagesToolChoice] = (
                    self._map_tool_choice(
                        tool_choice=non_default_params.get("tool_choice"),
                        parallel_tool_use=non_default_params.get("parallel_tool_calls"),
                    )
                )

                if _tool_choice is not None:
                    optional_params["tool_choice"] = _tool_choice
            if param == "stream" and value is True:
                optional_params["stream"] = value
            if param == "stop" and (isinstance(value, str) or isinstance(value, list)):
                _value = self._map_stop_sequences(value)
                if _value is not None:
                    optional_params["stop_sequences"] = _value
            if param == "temperature":
                optional_params["temperature"] = value
            if param == "top_p":
                optional_params["top_p"] = value
            if param == "response_format" and isinstance(value, dict):
                json_schema: Optional[dict] = None
                if "response_schema" in value:
                    json_schema = value["response_schema"]
                elif "json_schema" in value:
                    json_schema = value["json_schema"]["schema"]
                """
                When using tools in this way: - https://docs.anthropic.com/en/docs/build-with-claude/tool-use#json-mode
                - You usually want to provide a single tool
                - You should set tool_choice (see Forcing tool use) to instruct the model to explicitly use that tool
                - Remember that the model will pass the input to the tool, so the name of the tool and description should be from the model’s perspective.
                """
                _tool_choice = {"name": "json_tool_call", "type": "tool"}
                _tool = self._create_json_tool_call_for_response_format(
                    json_schema=json_schema,
                )
                optional_params["tools"] = [_tool]
                optional_params["tool_choice"] = _tool_choice
                optional_params["json_mode"] = True
            if param == "user":
                optional_params["metadata"] = {"user_id": value}

        return optional_params

    def _create_json_tool_call_for_response_format(
        self,
        json_schema: Optional[dict] = None,
    ) -> AnthropicMessagesTool:
        """
        Handles creating a tool call for getting responses in JSON format.

        Args:
            json_schema (Optional[dict]): The JSON schema the response should be in

        Returns:
            AnthropicMessagesTool: The tool call to send to Anthropic API to get responses in JSON format
        """
        _input_schema: AnthropicInputSchema = AnthropicInputSchema(
            type="object",
        )

        if json_schema is None:
            # Anthropic raises a 400 BadRequest error if properties is passed as None
            # see usage with additionalProperties (Example 5) https://github.com/anthropics/anthropic-cookbook/blob/main/tool_use/extracting_structured_json.ipynb
            _input_schema["additionalProperties"] = True
            _input_schema["properties"] = {}
        else:
            _input_schema["properties"] = {"values": json_schema}

        _tool = AnthropicMessagesTool(name="json_tool_call", input_schema=_input_schema)
        return _tool

    def is_cache_control_set(self, messages: List[AllMessageValues]) -> bool:
        """
        Return if {"cache_control": ..} in message content block

        Used to check if anthropic prompt caching headers need to be set.
        """
        for message in messages:
            if message.get("cache_control", None) is not None:
                return True
            _message_content = message.get("content")
            if _message_content is not None and isinstance(_message_content, list):
                for content in _message_content:
                    if "cache_control" in content:
                        return True

        return False

    def is_computer_tool_used(
        self, tools: Optional[List[AllAnthropicToolsValues]]
    ) -> bool:
        if tools is None:
            return False
        for tool in tools:
            if "type" in tool and tool["type"].startswith("computer_"):
                return True
        return False

    def is_pdf_used(self, messages: List[AllMessageValues]) -> bool:
        """
        Set to true if media passed into messages.

        """
        for message in messages:
            if (
                "content" in message
                and message["content"] is not None
                and isinstance(message["content"], list)
            ):
                for content in message["content"]:
                    if "type" in content and content["type"] != "text":
                        return True
        return False

    def translate_system_message(
        self, messages: List[AllMessageValues]
    ) -> List[AnthropicSystemMessageContent]:
        """
        Translate system message to anthropic format.

        Removes system message from the original list and returns a new list of anthropic system message content.
        """
        system_prompt_indices = []
        anthropic_system_message_list: List[AnthropicSystemMessageContent] = []
        for idx, message in enumerate(messages):
            if message["role"] == "system":
                valid_content: bool = False
                system_message_block = ChatCompletionSystemMessage(**message)
                if isinstance(system_message_block["content"], str):
                    anthropic_system_message_content = AnthropicSystemMessageContent(
                        type="text",
                        text=system_message_block["content"],
                    )
                    if "cache_control" in system_message_block:
                        anthropic_system_message_content["cache_control"] = (
                            system_message_block["cache_control"]
                        )
                    anthropic_system_message_list.append(
                        anthropic_system_message_content
                    )
                    valid_content = True
                elif isinstance(message["content"], list):
                    for _content in message["content"]:
                        anthropic_system_message_content = (
                            AnthropicSystemMessageContent(
                                type=_content.get("type"),
                                text=_content.get("text"),
                            )
                        )
                        if "cache_control" in _content:
                            anthropic_system_message_content["cache_control"] = (
                                _content["cache_control"]
                            )

                        anthropic_system_message_list.append(
                            anthropic_system_message_content
                        )
                    valid_content = True

                if valid_content:
                    system_prompt_indices.append(idx)
        if len(system_prompt_indices) > 0:
            for idx in reversed(system_prompt_indices):
                messages.pop(idx)

        return anthropic_system_message_list

    def transform_request(
        self,
        model: str,
        messages: List[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        """
        Translate messages to anthropic format.
        """
        ## VALIDATE REQUEST
        """
        Anthropic doesn't support tool calling without `tools=` param specified.
        """
        if (
            "tools" not in optional_params
            and messages is not None
            and has_tool_call_blocks(messages)
        ):
            if litellm.modify_params:
                optional_params["tools"] = self._map_tools(
                    add_dummy_tool(custom_llm_provider="anthropic")
                )
            else:
                raise litellm.UnsupportedParamsError(
                    message="Anthropic doesn't support tool calling without `tools=` param specified. Pass `tools=` param OR set `litellm.modify_params = True` // `litellm_settings::modify_params: True` to add dummy tool to the request.",
                    model="",
                    llm_provider="anthropic",
                )

        # Separate system prompt from rest of message
        anthropic_system_message_list = self.translate_system_message(messages=messages)
        # Handling anthropic API Prompt Caching
        if len(anthropic_system_message_list) > 0:
            optional_params["system"] = anthropic_system_message_list
        # Format rest of message according to anthropic guidelines
        try:
            anthropic_messages = anthropic_messages_pt(
                model=model,
                messages=messages,
                llm_provider="anthropic",
            )
        except Exception as e:
            raise AnthropicError(
                status_code=400,
                message="{}\nReceived Messages={}".format(str(e), messages),
            )  # don't use verbose_logger.exception, if exception is raised

        ## Load Config
        config = litellm.AnthropicConfig.get_config()
        for k, v in config.items():
            if (
                k not in optional_params
            ):  # completion(top_k=3) > anthropic_config(top_k=3) <- allows for dynamic variables to be passed in
                optional_params[k] = v

        ## Handle Tool Calling
        _is_function_call = False
        if "tools" in optional_params:
            _is_function_call = True

        # litellm params used internally for transformation of response
        json_mode: bool = optional_params.pop("json_mode", False)
        is_vertex_request: bool = optional_params.pop("is_vertex_request", False)
        litellm_params["json_mode"] = json_mode
        litellm_params["is_vertex_request"] = is_vertex_request
        litellm_params["_is_function_call"] = _is_function_call

        ## Handle user_id in metadata
        _litellm_metadata = litellm_params.get("metadata", None)
        if (
            _litellm_metadata
            and isinstance(_litellm_metadata, dict)
            and "user_id" in _litellm_metadata
        ):
            optional_params["metadata"] = {"user_id": _litellm_metadata["user_id"]}

        data = {
            "model": model,
            "messages": anthropic_messages,
            **optional_params,
        }

        return data

    def transform_response(
        self,
        model: str,
        raw_response: httpx.Response,
        model_response: ModelResponse,
        logging_obj: LiteLLMLoggingObj,
        request_data: Dict,
        messages: List[AllMessageValues],
        optional_params: Dict,
        encoding: Any,
        api_key: Optional[str] = None,
        json_mode: Optional[bool] = None,
    ) -> ModelResponse:
        _hidden_params: Dict = {}
        ## LOGGING
        logging_obj.post_call(
            input=messages,
            api_key=api_key,
            original_response=raw_response.text,
            additional_args={"complete_input_dict": request_data},
        )

        ## RESPONSE OBJECT
        try:
            completion_response = raw_response.json()
        except Exception as e:
            response_headers = getattr(raw_response, "headers", None)
            raise AnthropicError(
                message="Unable to get json response - {}, Original Response: {}".format(
                    str(e), raw_response.text
                ),
                status_code=raw_response.status_code,
                headers=response_headers,
            )
        if "error" in completion_response:
            response_headers = getattr(raw_response, "headers", None)
            raise AnthropicError(
                message=str(completion_response["error"]),
                status_code=raw_response.status_code,
                headers=response_headers,
            )
        else:
            text_content = ""
            tool_calls: List[ChatCompletionToolCallChunk] = []
            for idx, content in enumerate(completion_response["content"]):
                if content["type"] == "text":
                    text_content += content["text"]
                ## TOOL CALLING
                elif content["type"] == "tool_use":
                    tool_calls.append(
                        ChatCompletionToolCallChunk(
                            id=content["id"],
                            type="function",
                            function=ChatCompletionToolCallFunctionChunk(
                                name=content["name"],
                                arguments=json.dumps(content["input"]),
                            ),
                            index=idx,
                        )
                    )

            _message = litellm.Message(
                tool_calls=tool_calls,
                content=text_content or None,
            )

            ## HANDLE JSON MODE - anthropic returns single function call
            if json_mode is True and len(tool_calls) == 1:
                json_mode_content_str: Optional[str] = tool_calls[0]["function"].get(
                    "arguments"
                )
                if json_mode_content_str is not None:
                    _converted_message = (
                        AnthropicConfig._convert_tool_response_to_message(
                            tool_calls=tool_calls,
                        )
                    )
                    if _converted_message is not None:
                        completion_response["stop_reason"] = "stop"
                        _message = _converted_message
            model_response.choices[0].message = _message  # type: ignore
            model_response._hidden_params["original_response"] = completion_response[
                "content"
            ]  # allow user to access raw anthropic tool calling response

            model_response.choices[0].finish_reason = map_finish_reason(
                completion_response["stop_reason"]
            )

        ## CALCULATING USAGE
        prompt_tokens = completion_response["usage"]["input_tokens"]
        completion_tokens = completion_response["usage"]["output_tokens"]
        _usage = completion_response["usage"]
        cache_creation_input_tokens: int = 0
        cache_read_input_tokens: int = 0

        model_response.created = int(time.time())
        model_response.model = model
        if "cache_creation_input_tokens" in _usage:
            cache_creation_input_tokens = _usage["cache_creation_input_tokens"]
            prompt_tokens += cache_creation_input_tokens
        if "cache_read_input_tokens" in _usage:
            cache_read_input_tokens = _usage["cache_read_input_tokens"]
            prompt_tokens += cache_read_input_tokens

        prompt_tokens_details = PromptTokensDetailsWrapper(
            cached_tokens=cache_read_input_tokens
        )
        total_tokens = prompt_tokens + completion_tokens
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            prompt_tokens_details=prompt_tokens_details,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )

        setattr(model_response, "usage", usage)  # type: ignore

        model_response._hidden_params = _hidden_params
        return model_response

    @staticmethod
    def _convert_tool_response_to_message(
        tool_calls: List[ChatCompletionToolCallChunk],
    ) -> Optional[LitellmMessage]:
        """
        In JSON mode, Anthropic API returns JSON schema as a tool call, we need to convert it to a message to follow the OpenAI format

        """
        ## HANDLE JSON MODE - anthropic returns single function call
        json_mode_content_str: Optional[str] = tool_calls[0]["function"].get(
            "arguments"
        )
        try:
            if json_mode_content_str is not None:
                args = json.loads(json_mode_content_str)
                if (
                    isinstance(args, dict)
                    and (values := args.get("values")) is not None
                ):
                    _message = litellm.Message(content=json.dumps(values))
                    return _message
                else:
                    # a lot of the times the `values` key is not present in the tool response
                    # relevant issue: https://github.com/BerriAI/litellm/issues/6741
                    _message = litellm.Message(content=json.dumps(args))
                    return _message
        except json.JSONDecodeError:
            # json decode error does occur, return the original tool response str
            return litellm.Message(content=json_mode_content_str)
        return None

    def _transform_messages(
        self, messages: List[AllMessageValues]
    ) -> List[AllMessageValues]:
        return messages

    def get_error_class(
        self, error_message: str, status_code: int, headers: Union[Dict, httpx.Headers]
    ) -> BaseLLMException:
        return AnthropicError(
            status_code=status_code,
            message=error_message,
            headers=cast(httpx.Headers, headers),
        )

    def validate_environment(
        self,
        headers: dict,
        model: str,
        messages: List[AllMessageValues],
        optional_params: dict,
        api_key: Optional[str] = None,
    ) -> Dict:
        if api_key is None:
            raise litellm.AuthenticationError(
                message="Missing Anthropic API Key - A call is being made to anthropic but no key is set either in the environment variables or via params. Please set `ANTHROPIC_API_KEY` in your environment vars",
                llm_provider="anthropic",
                model=model,
            )

        tools = optional_params.get("tools")
        prompt_caching_set = self.is_cache_control_set(messages=messages)
        computer_tool_used = self.is_computer_tool_used(tools=tools)
        pdf_used = self.is_pdf_used(messages=messages)
        anthropic_headers = self.get_anthropic_headers(
            computer_tool_used=computer_tool_used,
            prompt_caching_set=prompt_caching_set,
            pdf_used=pdf_used,
            api_key=api_key,
            is_vertex_request=False,
        )

        headers = {**headers, **anthropic_headers}
        return headers

    def transform_response_headers(self, headers: Union[httpx.Headers, dict]) -> dict:
        """
        Transform the response headers from Anthropic API to the OpenAI API format

        OpenAI Headers are:
        `x-ratelimit-limit-requests`
        `x-ratelimit-remaining-requests`
        `x-ratelimit-limit-tokens`
        `x-ratelimit-remaining-tokens`
        """
        return process_anthropic_headers(headers)

    def get_model_response_iterator(
        self,
        streaming_response: Union[Iterator[str], AsyncIterator[str], ModelResponse],
        sync_stream: bool,
        json_mode: Optional[bool] = False,
    ):
        return AnthropicModelResponseIterator(
            streaming_response=streaming_response,
            sync_stream=sync_stream,
            json_mode=json_mode,
        )


class AnthropicModelResponseIterator(BaseModelResponseIterator):
    def __init__(
        self, streaming_response, sync_stream: bool, json_mode: Optional[bool] = False
    ):
        self.streaming_response = streaming_response
        self.response_iterator = self.streaming_response
        self.content_blocks: List[ContentBlockDelta] = []
        self.tool_index = -1
        self.json_mode = json_mode
        super().__init__(
            streaming_response=streaming_response,
            sync_stream=sync_stream,
            json_mode=json_mode,
        )

    def check_empty_tool_call_args(self) -> bool:
        """
        Check if the tool call block so far has been an empty string
        """
        args = ""
        # if text content block -> skip
        if len(self.content_blocks) == 0:
            return False

        if self.content_blocks[0]["delta"]["type"] == "text_delta":
            return False

        for block in self.content_blocks:
            if block["delta"]["type"] == "input_json_delta":
                args += block["delta"].get("partial_json", "")  # type: ignore

        if len(args) == 0:
            return True
        return False

    def _handle_usage(
        self, anthropic_usage_chunk: Union[dict, UsageDelta]
    ) -> AnthropicChatCompletionUsageBlock:

        usage_block = AnthropicChatCompletionUsageBlock(
            prompt_tokens=anthropic_usage_chunk.get("input_tokens", 0),
            completion_tokens=anthropic_usage_chunk.get("output_tokens", 0),
            total_tokens=anthropic_usage_chunk.get("input_tokens", 0)
            + anthropic_usage_chunk.get("output_tokens", 0),
        )

        cache_creation_input_tokens = anthropic_usage_chunk.get(
            "cache_creation_input_tokens"
        )
        if cache_creation_input_tokens is not None and isinstance(
            cache_creation_input_tokens, int
        ):
            usage_block["cache_creation_input_tokens"] = cache_creation_input_tokens

        cache_read_input_tokens = anthropic_usage_chunk.get("cache_read_input_tokens")
        if cache_read_input_tokens is not None and isinstance(
            cache_read_input_tokens, int
        ):
            usage_block["cache_read_input_tokens"] = cache_read_input_tokens

        return usage_block

    def chunk_parser(self, chunk: dict) -> GenericStreamingChunk:
        try:
            type_chunk = chunk.get("type", "") or ""

            text = ""
            tool_use: Optional[ChatCompletionToolCallChunk] = None
            is_finished = False
            finish_reason = ""
            usage: Optional[ChatCompletionUsageBlock] = None

            index = int(chunk.get("index", 0))
            if type_chunk == "content_block_delta":
                """
                Anthropic content chunk
                chunk = {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Hello'}}
                """
                content_block = ContentBlockDelta(**chunk)  # type: ignore
                self.content_blocks.append(content_block)
                if "text" in content_block["delta"]:
                    text = content_block["delta"]["text"]
                elif "partial_json" in content_block["delta"]:
                    tool_use = {
                        "id": None,
                        "type": "function",
                        "function": {
                            "name": None,
                            "arguments": content_block["delta"]["partial_json"],
                        },
                        "index": self.tool_index,
                    }
            elif type_chunk == "content_block_start":
                """
                event: content_block_start
                data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_01T1x1fJ34qAmk2tNTrN7Up6","name":"get_weather","input":{}}}
                """
                content_block_start = ContentBlockStart(**chunk)  # type: ignore
                self.content_blocks = []  # reset content blocks when new block starts
                if content_block_start["content_block"]["type"] == "text":
                    text = content_block_start["content_block"]["text"]
                elif content_block_start["content_block"]["type"] == "tool_use":
                    self.tool_index += 1
                    tool_use = {
                        "id": content_block_start["content_block"]["id"],
                        "type": "function",
                        "function": {
                            "name": content_block_start["content_block"]["name"],
                            "arguments": "",
                        },
                        "index": self.tool_index,
                    }
            elif type_chunk == "content_block_stop":
                ContentBlockStop(**chunk)  # type: ignore
                # check if tool call content block
                is_empty = self.check_empty_tool_call_args()
                if is_empty:
                    tool_use = {
                        "id": None,
                        "type": "function",
                        "function": {
                            "name": None,
                            "arguments": "{}",
                        },
                        "index": self.tool_index,
                    }
            elif type_chunk == "message_delta":
                """
                Anthropic
                chunk = {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens', 'stop_sequence': None}, 'usage': {'output_tokens': 10}}
                """
                # TODO - get usage from this chunk, set in response
                message_delta = MessageBlockDelta(**chunk)  # type: ignore
                finish_reason = map_finish_reason(
                    finish_reason=message_delta["delta"].get("stop_reason", "stop")
                    or "stop"
                )
                usage = self._handle_usage(anthropic_usage_chunk=message_delta["usage"])
                is_finished = True
            elif type_chunk == "message_start":
                """
                Anthropic
                chunk = {
                    "type": "message_start",
                    "message": {
                        "id": "msg_vrtx_011PqREFEMzd3REdCoUFAmdG",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-3-sonnet-20240229",
                        "content": [],
                        "stop_reason": null,
                        "stop_sequence": null,
                        "usage": {
                            "input_tokens": 270,
                            "output_tokens": 1
                        }
                    }
                }
                """
                message_start_block = MessageStartBlock(**chunk)  # type: ignore
                if "usage" in message_start_block["message"]:
                    usage = self._handle_usage(
                        anthropic_usage_chunk=message_start_block["message"]["usage"]
                    )
            elif type_chunk == "error":
                """
                {"type":"error","error":{"details":null,"type":"api_error","message":"Internal server error"}      }
                """
                _error_dict = chunk.get("error", {}) or {}
                message = _error_dict.get("message", None) or str(chunk)
                raise AnthropicError(
                    message=message,
                    status_code=500,  # it looks like Anthropic API does not return a status code in the chunk error - default to 500
                )

            text, tool_use = self._handle_json_mode_chunk(text=text, tool_use=tool_use)

            returned_chunk = GenericStreamingChunk(
                text=text,
                tool_use=tool_use,
                is_finished=is_finished,
                finish_reason=finish_reason,
                usage=usage,
                index=index,
            )

            return returned_chunk

        except json.JSONDecodeError:
            raise ValueError(f"Failed to decode JSON from chunk: {chunk}")

    def _handle_json_mode_chunk(
        self, text: str, tool_use: Optional[ChatCompletionToolCallChunk]
    ) -> Tuple[str, Optional[ChatCompletionToolCallChunk]]:
        """
        If JSON mode is enabled, convert the tool call to a message.

        Anthropic returns the JSON schema as part of the tool call
        OpenAI returns the JSON schema as part of the content, this handles placing it in the content

        Args:
            text: str
            tool_use: Optional[ChatCompletionToolCallChunk]
        Returns:
            Tuple[str, Optional[ChatCompletionToolCallChunk]]

            text: The text to use in the content
            tool_use: The ChatCompletionToolCallChunk to use in the chunk response
        """
        if self.json_mode is True and tool_use is not None:
            message = AnthropicConfig._convert_tool_response_to_message(
                tool_calls=[tool_use]
            )
            if message is not None:
                text = message.content or ""
                tool_use = None

        return text, tool_use

    def convert_str_chunk_to_generic_chunk(self, chunk: str) -> GenericStreamingChunk:
        """
        Convert a string chunk to a GenericStreamingChunk

        Note: This is used for Anthropic pass through streaming logging

        We can move __anext__, and __next__ to use this function since it's common logic.
        Did not migrate them to minmize changes made in 1 PR.
        """
        str_line = chunk
        if isinstance(chunk, bytes):  # Handle binary data
            str_line = chunk.decode("utf-8")  # Convert bytes to string
            index = str_line.find("data:")
            if index != -1:
                str_line = str_line[index:]

        if str_line.startswith("data:"):
            data_json = json.loads(str_line[5:])
            return self.chunk_parser(chunk=data_json)
        else:
            return GenericStreamingChunk(
                text="",
                is_finished=False,
                finish_reason="",
                usage=None,
                index=0,
                tool_use=None,
            )
