"""
Translates from OpenAI's `/v1/chat/completions` to Databricks' `/chat/completions`
"""

import json
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
    cast,
)

import httpx
from pydantic import BaseModel

from litellm.litellm_core_utils.llm_response_utils.convert_dict_to_response import (
    convert_to_model_response_object,
)
from litellm.litellm_core_utils.prompt_templates.common_utils import (
    handle_messages_with_content_list_to_str_conversion,
    strip_name_from_messages,
)
from litellm.llms.base_llm.base_model_iterator import BaseModelResponseIterator
from litellm.types.llms.databricks import (
    AllDatabricksContentValues,
    DatabricksChoice,
    DatabricksResponse,
)
from litellm.types.llms.openai import AllMessageValues, ChatCompletionThinkingBlock
from litellm.types.utils import (
    Choices,
    Message,
    ModelResponse,
    ModelResponseStream,
    ProviderField,
    StreamingChoices,
    Usage,
)

from ...openai_like.chat.transformation import OpenAILikeChatConfig
from ..common_utils import DatabricksBase, DatabricksException

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as _LiteLLMLoggingObj

    LiteLLMLoggingObj = _LiteLLMLoggingObj
else:
    LiteLLMLoggingObj = Any


class DatabricksConfig(DatabricksBase, OpenAILikeChatConfig):
    """
    Reference: https://docs.databricks.com/en/machine-learning/foundation-models/api-reference.html#chat-request
    """

    max_tokens: Optional[int] = None
    temperature: Optional[int] = None
    top_p: Optional[int] = None
    top_k: Optional[int] = None
    stop: Optional[Union[List[str], str]] = None
    n: Optional[int] = None

    def __init__(
        self,
        max_tokens: Optional[int] = None,
        temperature: Optional[int] = None,
        top_p: Optional[int] = None,
        top_k: Optional[int] = None,
        stop: Optional[Union[List[str], str]] = None,
        n: Optional[int] = None,
    ) -> None:
        locals_ = locals().copy()
        for key, value in locals_.items():
            if key != "self" and value is not None:
                setattr(self.__class__, key, value)

    @classmethod
    def get_config(cls):
        return super().get_config()

    def get_required_params(self) -> List[ProviderField]:
        """For a given provider, return it's required fields with a description"""
        return [
            ProviderField(
                field_name="api_key",
                field_type="string",
                field_description="Your Databricks API Key.",
                field_value="dapi...",
            ),
            ProviderField(
                field_name="api_base",
                field_type="string",
                field_description="Your Databricks API Base.",
                field_value="https://adb-..",
            ),
        ]

    def validate_environment(
        self,
        headers: dict,
        model: str,
        messages: List[AllMessageValues],
        optional_params: dict,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> dict:
        api_base, headers = self.databricks_validate_environment(
            api_base=api_base,
            api_key=api_key,
            endpoint_type="chat_completions",
            custom_endpoint=False,
            headers=headers,
        )
        return headers

    def get_complete_url(
        self,
        api_base: Optional[str],
        api_key: Optional[str],
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: Optional[bool] = None,
    ) -> str:
        api_base = self._get_api_base(api_base)
        complete_url = f"{api_base}/chat/completions"
        return complete_url

    def get_supported_openai_params(self, model: Optional[str] = None) -> list:
        return [
            "stream",
            "stop",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "n",
            "response_format",
            "tools",
            "tool_choice",
            "reasoning_effort",
            "thinking",
        ]

    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
        replace_max_completion_tokens_with_max_tokens: bool = True,
    ) -> dict:
        mapped_params = super().map_openai_params(
            non_default_params, optional_params, model, drop_params
        )
        if (
            "max_completion_tokens" in non_default_params
            and replace_max_completion_tokens_with_max_tokens
        ):
            mapped_params["max_tokens"] = non_default_params[
                "max_completion_tokens"
            ]  # most openai-compatible providers support 'max_tokens' not 'max_completion_tokens'
            mapped_params.pop("max_completion_tokens", None)

        ## handle thinking tokens
        self.update_optional_params_with_thinking_tokens(
            non_default_params=non_default_params, optional_params=optional_params
        )
        return mapped_params

    def _should_fake_stream(self, optional_params: dict) -> bool:
        """
        Databricks doesn't support 'response_format' while streaming
        """
        if optional_params.get("response_format") is not None:
            return True

        return False

    def _transform_messages(
        self, messages: List[AllMessageValues], model: str
    ) -> List[AllMessageValues]:
        """
        Databricks does not support:
        - content in list format.
        - 'name' in user message.
        """
        new_messages = []
        for idx, message in enumerate(messages):
            if isinstance(message, BaseModel):
                _message = message.model_dump(exclude_none=True)
            else:
                _message = message
            new_messages.append(_message)
        new_messages = handle_messages_with_content_list_to_str_conversion(new_messages)
        new_messages = strip_name_from_messages(new_messages)
        return super()._transform_messages(messages=new_messages, model=model)

    @staticmethod
    def extract_content_str(content: AllDatabricksContentValues) -> str:
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            content_str = ""
            for item in content:
                if item["type"] == "text":
                    content_str += item["text"]
            return content_str
        else:
            raise Exception(f"Unsupported content type: {type(content)}")

    @staticmethod
    def extract_reasoning_content(
        content: AllDatabricksContentValues,
    ) -> Tuple[Optional[str], Optional[List[ChatCompletionThinkingBlock]]]:
        """
        Extract and return the reasoning content and thinking blocks
        """
        thinking_blocks: Optional[List[ChatCompletionThinkingBlock]] = None
        reasoning_content: Optional[str] = None
        if isinstance(content, list):
            for item in content:
                if item["type"] == "reasoning":
                    for sum in item["summary"]:
                        if reasoning_content is None:
                            reasoning_content = ""
                        reasoning_content += sum["text"]
                        thinking_block = ChatCompletionThinkingBlock(
                            type="thinking",
                            thinking=sum["text"],
                            signature=sum["signature"],
                        )
                        if thinking_blocks is None:
                            thinking_blocks = []
                        thinking_blocks.append(thinking_block)
        return reasoning_content, thinking_blocks

    def _transform_choices(self, choices: List[DatabricksChoice]) -> List[Choices]:
        transformed_choices = []
        for choice in choices:
            ## get the content str
            content_str = DatabricksConfig.extract_content_str(
                choice["message"]["content"]
            )

            ## get the reasoning content
            (
                reasoning_content,
                thinking_blocks,
            ) = DatabricksConfig.extract_reasoning_content(choice["message"]["content"])

            translated_message = Message(
                role="assistant",
                content=content_str,
                reasoning_content=reasoning_content,
                thinking_blocks=thinking_blocks,
                tool_calls=choice["message"].get("tool_calls"),
            )

            translated_choice = Choices(
                finish_reason=choice["finish_reason"],
                index=choice["index"],
                message=translated_message,
                logprobs=None,
                enhancements=None,
            )

            transformed_choices.append(translated_choice)

        return transformed_choices

    def transform_response(
        self,
        model: str,
        raw_response: httpx.Response,
        model_response: ModelResponse,
        logging_obj: LiteLLMLoggingObj,
        request_data: dict,
        messages: List[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        encoding: Any,
        api_key: Optional[str] = None,
        json_mode: Optional[bool] = None,
    ) -> ModelResponse:
        ## LOGGING
        logging_obj.post_call(
            input=messages,
            api_key=api_key,
            original_response=raw_response.text,
            additional_args={"complete_input_dict": request_data},
        )

        ## RESPONSE OBJECT
        try:
            completion_response = DatabricksResponse(**raw_response.json())
        except Exception as e:
            response_headers = getattr(raw_response, "headers", None)
            raise DatabricksException(
                message="Unable to get json response - {}, Original Response: {}".format(
                    str(e), raw_response.text
                ),
                status_code=raw_response.status_code,
                headers=response_headers,
            )

        model_response.model = completion_response["model"]
        model_response.id = completion_response["id"]
        model_response.created = completion_response["created"]
        setattr(model_response, "usage", Usage(**completion_response["usage"]))

        model_response.choices = self._transform_choices(  # type: ignore
            choices=completion_response["choices"]
        )

        return model_response

    def get_model_response_iterator(
        self,
        streaming_response: Union[Iterator[str], AsyncIterator[str], ModelResponse],
        sync_stream: bool,
        json_mode: Optional[bool] = False,
    ):
        return DatabricksChatResponseIterator(
            streaming_response=streaming_response,
            sync_stream=sync_stream,
            json_mode=json_mode,
        )


class DatabricksChatResponseIterator(BaseModelResponseIterator):
    def chunk_parser(self, chunk: dict) -> ModelResponseStream:
        try:
            translated_choices = []
            for choice in chunk["choices"]:
                # extract the content str
                content_str = DatabricksConfig.extract_content_str(
                    choice["delta"]["content"]
                )

                # extract the reasoning content
                (
                    reasoning_content,
                    thinking_blocks,
                ) = DatabricksConfig.extract_reasoning_content(
                    choice["delta"]["content"]
                )

                choice["delta"]["content"] = content_str
                choice["delta"]["reasoning_content"] = reasoning_content
                choice["delta"]["thinking_blocks"] = thinking_blocks
                translated_choices.append(choice)
            return ModelResponseStream(
                id=chunk["id"],
                object="chat.completion.chunk",
                created=chunk["created"],
                model=chunk["model"],
                choices=translated_choices,
            )
        except KeyError as e:
            raise DatabricksException(
                message=f"KeyError: {e}, Got unexpected response from Databricks: {chunk}",
                status_code=400,
            )
        except Exception as e:
            raise e
