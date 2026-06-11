import json
import logging
import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

"""Wrapper around AWS Bedrock for Claude model invocations."""
class BedrockLLM:
    def __init__(
        self,
        model: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        region_name: str = "us-west-1",
        # Cap output tokens to something reasonable for a support answer
        max_tokens: int = 4096,
    ):
        self.model = model
        self.max_tokens = max_tokens

        config = Config(
            read_timeout=900,
            connect_timeout=60,
            retries={'max_attempts': 3, 'mode': 'adaptive'}
        )

        self.client = boto3.client(
            service_name="bedrock-runtime",
            region_name=region_name,
            config=config)

    def invoke(self, prompt: str) -> dict:
        """Invoke the model and return text + usage metadata.

        Returns:
            dict with keys: text, input_tokens, output_tokens
        """
        logger.info("Bedrock request started — model: %s, prompt length: %d chars", self.model, len(prompt))
        response = self.client.invoke_model(
            modelId=self.model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": self.max_tokens,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
            }),
        )

        result = json.loads(response["body"].read())

        usage = result.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        print(
            f"Tokens — input: {input_tokens}, "
            f"output: {output_tokens}"
        )

        stop_reason = result.get("stop_reason")
        if stop_reason:
            if stop_reason == "max_tokens":
                print("WARNING: Response was truncated — increase max_tokens")
            if stop_reason != "end_turn":
                print(f"Stop reason: {stop_reason}")

        return {
            "text": result["content"][0]["text"],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    def invoke_stream(self, prompt: str):
        """Invoke the model with streaming. Yields text chunks as they arrive.

        Yields:
            str: text delta chunks

        After iteration completes, access .last_usage for token counts:
            {"input_tokens": int, "output_tokens": int}
        """
        logger.info("Bedrock stream request started — model: %s, prompt length: %d chars", self.model, len(prompt))
        response = self.client.invoke_model_with_response_stream(
            modelId=self.model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": self.max_tokens,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
            }),
        )

        self.last_usage = {"input_tokens": 0, "output_tokens": 0}

        for event in response["body"]:
            chunk = json.loads(event["chunk"]["bytes"])
            chunk_type = chunk.get("type")

            if chunk_type == "content_block_delta":
                delta = chunk.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield delta["text"]
            elif chunk_type == "message_delta":
                usage = chunk.get("usage", {})
                self.last_usage["output_tokens"] = usage.get("output_tokens", 0)
            elif chunk_type == "message_start":
                usage = chunk.get("message", {}).get("usage", {})
                self.last_usage["input_tokens"] = usage.get("input_tokens", 0)
