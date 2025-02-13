from typing import Optional, Tuple


from litellm.llms.openai.openai import OpenAIConfig

from ..authenticator import Authenticator

class GithubCopilotConfig(OpenAIConfig):
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        custom_llm_provider: str = "openai",
    ) -> None:
        super().__init__()

        self.authenticator = Authenticator()

    def _get_openai_compatible_provider_info(
        self,
        model: str,
        api_base: Optional[str],
        api_key: Optional[str],
        custom_llm_provider: str,
    ) -> Tuple[Optional[str], Optional[str], str]:
        api_base = "https://api.githubcopilot.com"
        dynamic_api_key = self.authenticator.get_api_key()
        
        return api_base, dynamic_api_key, custom_llm_provider
