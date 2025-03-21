from typing import Dict, Optional

import litellm
from litellm._logging import verbose_logger
from litellm.secret_managers.main import get_secret_str


class PassthroughEndpointRouter:
    """
    CRUD operations for pass-through endpoints
    """

    def __init__(self):
        self.credentials: Dict[str, Dict[str, str]] = {}

    def add_credentials(
        self,
        custom_llm_provider: str,
        api_base: Optional[str],
        api_key: Optional[str],
        credential_name: Optional[str] = None,
        credential_values: Optional[Dict[str, str]] = None,
    ):
        """
        Set credentials for a pass-through endpoint. Used when a user adds a pass-through LLM endpoint on the UI.

        Args:
            custom_llm_provider: The provider of the pass-through endpoint
            api_base: The base URL of the pass-through endpoint
            api_key: The API key for the pass-through endpoint
            credential_values: A dictionary of credential values for the pass-through endpoint
        """
        credential_name = credential_name or self._get_credential_name_for_provider(
            custom_llm_provider=custom_llm_provider,
            region_name=(
                self._get_region_name_from_api_base(
                    api_base=api_base, custom_llm_provider=custom_llm_provider
                )
            ),
        )

        _credential_values = {}
        if api_key is not None:
            _credential_values["api_key"] = api_key
        if credential_values is not None:
            _credential_values.update(credential_values)
        self.credentials[credential_name] = _credential_values

    def update_credentials(
        self,
        credential_name: str,
        credential_values: Dict[str, str],
    ):
        self.credentials[credential_name] = credential_values

    def upsert_credentials(
        self,
        credential_name: str,
        custom_llm_provider: str,
        credential_values: Dict[str, str],
    ):
        is_new_credential = (
            self.get_credentials(
                custom_llm_provider=custom_llm_provider,
                region_name=None,
                credential_name=credential_name,
            )
            is None
        )
        if is_new_credential:
            self.add_credentials(
                custom_llm_provider=custom_llm_provider,
                api_base=None,
                api_key=None,
                credential_name=credential_name,
                credential_values=credential_values,
            )
        else:
            self.update_credentials(
                credential_name=credential_name,
                credential_values=credential_values,
            )

    def delete_credentials(
        self,
        credential_name: str,
    ):
        self.credentials.pop(credential_name)

    def get_credentials(
        self,
        custom_llm_provider: str,
        region_name: Optional[str],
        credential_name: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        if credential_name is None:
            credential_name = self._get_credential_name_for_provider(
                custom_llm_provider=custom_llm_provider,
                region_name=region_name,
            )

        verbose_logger.debug(
            f"Pass-through llm endpoints router, looking for credentials for {credential_name}. Credentials: {self.credentials.keys()}"
        )
        if credential_name in self.credentials:
            verbose_logger.debug(f"Found credentials for {credential_name}")
            return self.credentials[credential_name]
        elif custom_llm_provider is not None:
            verbose_logger.debug(
                f"No credentials found for {credential_name}, looking for env variable"
            )
            _env_variable_name = (
                self._get_default_env_variable_name_passthrough_endpoint(
                    custom_llm_provider=custom_llm_provider,
                )
            )
            _api_key = get_secret_str(_env_variable_name)
            if _api_key is not None:
                return {"api_key": _api_key}
        return None

    def _get_credential_from_credential_list(
        self,
        custom_llm_provider: str,
        region_name: Optional[str],
    ) -> Optional[str]:
        for credential in litellm.credential_list:
            if (
                credential.credential_info.get("custom_llm_provider")
                == custom_llm_provider
                and credential.credential_info.get("use_in_pass_through") is True
            ):
                return credential.credential_name
        return None

    def _get_credential_name_for_provider(
        self,
        custom_llm_provider: str,
        region_name: Optional[str],
    ) -> str:
        _credential_from_list = self._get_credential_from_credential_list(
            custom_llm_provider=custom_llm_provider,
            region_name=region_name,
        )
        if _credential_from_list is not None:
            return _credential_from_list
        elif region_name is None:
            return f"{custom_llm_provider.upper()}_API_KEY"
        else:
            return f"{custom_llm_provider.upper()}_{region_name.upper()}_API_KEY"

    def _get_region_name_from_api_base(
        self,
        custom_llm_provider: str,
        api_base: Optional[str],
    ) -> Optional[str]:
        """
        Get the region name from the API base.

        Each provider might have a different way of specifying the region in the API base - this is where you can use conditional logic to handle that.
        """
        if custom_llm_provider == "assemblyai":
            if api_base and "eu" in api_base:
                return "eu"
        return None

    @staticmethod
    def _get_default_env_variable_name_passthrough_endpoint(
        custom_llm_provider: str,
    ) -> str:
        return f"{custom_llm_provider.upper()}_API_KEY"
