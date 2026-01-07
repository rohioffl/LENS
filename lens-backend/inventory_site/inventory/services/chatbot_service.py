import os
import json
from typing import List, Dict, Optional
import requests


class ChatbotService:
    """Service for handling chatbot interactions with Google Gemini"""

    def __init__(self):
        self.api_key = None
        self._configure_client()

        # System prompt that defines the chatbot's behavior
        self.system_prompt = """You are a helpful AI assistant for a cloud infrastructure management platform.
You help users with:
- AWS and GCP cloud infrastructure questions
- Terraform and infrastructure as code
- VPN and networking configurations
- ECS, EKS, and GKE container orchestration
- Cloud migration strategies
- Security audits and best practices

Be concise, technical, and helpful. Provide code examples when relevant.
Respond with a short summary in at most 10 lines."""

    def _get_api_key(self) -> Optional[str]:
        return os.environ.get("GEMINI_API_KEY") or os.environ.get("ECS_MANIFEST_GEMINI_API_KEY_OVERRIDE")

    def _get_model_name(self) -> Optional[str]:
        model = (os.environ.get("CHATBOT_GEMINI_MODEL") or "").strip()
        if not model:
            return None
        if model and not model.startswith("models/"):
            return f"models/{model}"
        return model

    def _configure_client(self) -> None:
        api_key = (self._get_api_key() or "").strip()
        if not api_key:
            self.api_key = None
            return
        self.api_key = api_key

    def _convert_messages_to_gemini_format(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Convert messages from OpenAI format to Gemini format"""
        gemini_messages = []
        system_instruction = None

        for msg in messages:
            role = msg.get('role', '')
            content = msg.get('content', '')

            if role == 'system':
                system_instruction = content
            elif role == 'user':
                gemini_messages.append({'role': 'user', 'parts': [{'text': content}]})
            elif role == 'assistant':
                gemini_messages.append({'role': 'model', 'parts': [{'text': content}]})

        return gemini_messages, system_instruction

    def _build_request_payload(
        self,
        contents: List[Dict[str, str]],
        system_instruction: Optional[str],
        temperature: float,
        max_tokens: int
    ) -> Dict[str, object]:
        if system_instruction and contents:
            contents = [
                {
                    "role": "user",
                    "parts": [{"text": f"{system_instruction}\n\n{contents[0]['parts'][0]['text']}"}],
                }
            ] + contents[1:]
        payload: Dict[str, object] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        return payload

    def _build_url(self, model: str, streaming: bool) -> str:
        model_id = model.replace("models/", "")
        method = "streamGenerateContent" if streaming else "generateContent"
        base = f"https://generativelanguage.googleapis.com/v1/models/{model_id}:{method}"
        if streaming:
            return f"{base}?alt=sse&key={self.api_key}"
        return f"{base}?key={self.api_key}"

    def _handle_error(self, response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        message = ""
        if isinstance(payload, dict):
            message = payload.get("error", {}).get("message", "")
        error_text = message or response.text
        error_msg = error_text.lower()
        if 'api key' in error_msg or 'authentication' in error_msg:
            return "Warning: Invalid Gemini API key. Please check your configuration at https://makersuite.google.com/app/apikey"
        if 'quota' in error_msg or 'rate limit' in error_msg:
            return "Warning: Rate limit exceeded. Please try again in a moment."
        if 'safety' in error_msg:
            return "Warning: Response blocked by safety filters. Please rephrase your question."
        return f"Warning: Gemini API error: {error_text}"

    def get_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000
    ) -> str:
        """
        Get a completion from Google Gemini API

        Args:
            messages: List of message dicts with 'role' and 'content'
            model: Gemini model to use (gemini-pro, gemini-1.5-pro, gemini-1.5-flash)
            temperature: Creativity level (0-2 for Gemini)
            max_tokens: Maximum response length

        Returns:
            The assistant's response text
        """
        self._configure_client()
        model = self._get_model_name()
        if not self.api_key:
            return "Warning: Chatbot is not configured. Please set GEMINI_API_KEY."
        if not model:
            return "Warning: Chatbot model not configured. Please set CHATBOT_GEMINI_MODEL."

        try:
            gemini_messages, system_instruction = self._convert_messages_to_gemini_format(messages)
            merged_instruction = self.system_prompt
            if system_instruction:
                merged_instruction = f"{self.system_prompt}\n\n{system_instruction}"
            payload = self._build_request_payload(
                gemini_messages,
                merged_instruction,
                temperature,
                max_tokens
            )
            url = self._build_url(model, streaming=False)
            response = requests.post(url, json=payload, timeout=60)
            if response.status_code != 200:
                return self._handle_error(response)
            data = response.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return "Warning: Gemini API returned no candidates."
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                return "Warning: Gemini API returned an empty response."
            return (parts[0].get("text") or "").strip()

        except Exception as e:
            return f"Warning: Gemini API error: {str(e)}"

    def get_streaming_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000
    ):
        """
        Get a streaming completion from Google Gemini API

        Args:
            messages: List of message dicts with 'role' and 'content'
            model: Gemini model to use
            temperature: Creativity level (0-2)
            max_tokens: Maximum response length

        Yields:
            Chunks of the assistant's response
        """
        self._configure_client()
        model = self._get_model_name()
        if not self.api_key:
            yield "Warning: Chatbot is not configured. Please set GEMINI_API_KEY."
            return
        if not model:
            yield "Warning: Chatbot model not configured. Please set CHATBOT_GEMINI_MODEL."
            return

        try:
            gemini_messages, system_instruction = self._convert_messages_to_gemini_format(messages)
            merged_instruction = self.system_prompt
            if system_instruction:
                merged_instruction = f"{self.system_prompt}\n\n{system_instruction}"
            payload = self._build_request_payload(
                gemini_messages,
                merged_instruction,
                temperature,
                max_tokens
            )
            url = self._build_url(model, streaming=True)
            response = requests.post(url, json=payload, stream=True, timeout=60)
            if response.status_code != 200:
                yield self._handle_error(response)
                return
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                candidates = payload.get("candidates", [])
                if not candidates:
                    continue
                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    continue
                text = parts[0].get("text")
                if text:
                    yield text

        except Exception as e:
            yield f"Warning: Gemini API error: {str(e)}"


# Global chatbot service instance
chatbot_service = ChatbotService()
