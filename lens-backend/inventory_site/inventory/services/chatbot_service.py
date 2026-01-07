import os
import json
from typing import List, Dict, Optional
import google.generativeai as genai


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

    def _get_model_name(self, requested: str) -> str:
        env_model = (os.environ.get("CHATBOT_GEMINI_MODEL") or "").strip()
        return env_model or requested

    def _configure_client(self) -> None:
        api_key = (self._get_api_key() or "").strip()
        if not api_key:
            self.api_key = None
            return
        if api_key != self.api_key:
            genai.configure(api_key=api_key)
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
                gemini_messages.append({'role': 'user', 'parts': [content]})
            elif role == 'assistant':
                gemini_messages.append({'role': 'model', 'parts': [content]})

        return gemini_messages, system_instruction

    def get_completion(
        self,
        messages: List[Dict[str, str]],
        model: str = "gemini-1.5-flash",
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
        if not self.api_key:
            return "Warning: Chatbot is not configured. Please set GEMINI_API_KEY."

        try:
            model = self._get_model_name(model)
            gemini_messages, system_instruction = self._convert_messages_to_gemini_format(messages)

            # Create model with system instruction
            generation_config = {
                'temperature': temperature,
                'max_output_tokens': max_tokens,
            }

            if system_instruction:
                merged_instruction = f"{self.system_prompt}\n\n{system_instruction}"
                gemini_model = genai.GenerativeModel(
                    model_name=model,
                    generation_config=generation_config,
                    system_instruction=merged_instruction
                )
            else:
                gemini_model = genai.GenerativeModel(
                    model_name=model,
                    generation_config=generation_config
                )

            # Start chat with history
            chat = gemini_model.start_chat(history=gemini_messages[:-1] if len(gemini_messages) > 1 else [])

            # Send the last message
            last_message = gemini_messages[-1]['parts'][0] if gemini_messages else ""
            response = chat.send_message(last_message)

            return response.text.strip()

        except Exception as e:
            error_msg = str(e).lower()
            if 'api key' in error_msg or 'authentication' in error_msg:
                return "Warning: Invalid Gemini API key. Please check your configuration at https://makersuite.google.com/app/apikey"
            elif 'quota' in error_msg or 'rate limit' in error_msg:
                return "Warning: Rate limit exceeded. Please try again in a moment."
            elif 'safety' in error_msg:
                return "Warning: Response blocked by safety filters. Please rephrase your question."
            else:
                return f"Warning: Gemini API error: {str(e)}"

    def get_streaming_completion(
        self,
        messages: List[Dict[str, str]],
        model: str = "gemini-1.5-flash",
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
        if not self.api_key:
            yield "Warning: Chatbot is not configured. Please set GEMINI_API_KEY."
            return

        try:
            model = self._get_model_name(model)
            gemini_messages, system_instruction = self._convert_messages_to_gemini_format(messages)

            # Create model with system instruction
            generation_config = {
                'temperature': temperature,
                'max_output_tokens': max_tokens,
            }

            if system_instruction:
                merged_instruction = f"{self.system_prompt}\n\n{system_instruction}"
                gemini_model = genai.GenerativeModel(
                    model_name=model,
                    generation_config=generation_config,
                    system_instruction=merged_instruction
                )
            else:
                gemini_model = genai.GenerativeModel(
                    model_name=model,
                    generation_config=generation_config
                )

            # Start chat with history
            chat = gemini_model.start_chat(history=gemini_messages[:-1] if len(gemini_messages) > 1 else [])

            # Send the last message with streaming
            last_message = gemini_messages[-1]['parts'][0] if gemini_messages else ""
            response = chat.send_message(last_message, stream=True)

            for chunk in response:
                if chunk.text:
                    yield chunk.text

        except Exception as e:
            error_msg = str(e).lower()
            if 'api key' in error_msg or 'authentication' in error_msg:
                yield "Warning: Invalid Gemini API key. Please check your configuration at https://makersuite.google.com/app/apikey"
            elif 'quota' in error_msg or 'rate limit' in error_msg:
                yield "Warning: Rate limit exceeded. Please try again in a moment."
            elif 'safety' in error_msg:
                yield "Warning: Response blocked by safety filters. Please rephrase your question."
            else:
                yield f"Warning: Gemini API error: {str(e)}"


# Global chatbot service instance
chatbot_service = ChatbotService()
