import json
import boto3
from botocore.config import Config

"""Wrapper around AWS Bedrock for Claude model invocations."""
class BedrockLLM:
    def __init__(
        self,
        model: str = "anthropic.claude-sonnet-4-6",
        region_name: str = "us-west-1",
        # this is the maximum number of output tokens allowed for the model.
        max_tokens: int = 65536,
    ):
        self.model = model
        self.max_tokens = max_tokens

        config = Config(
            read_timeout=900,
            connect_timeout=60,
            retries={'max_attempts': 0} # Optional: avoid auto-retrying on timeout
        )

        self.client = boto3.client(
            service_name="bedrock-runtime",
            region_name=region_name,
            config=config)

    def invoke(self, prompt: str) -> str:
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
        print(
            f"Tokens — input: {usage.get('input_tokens', '?')}, "
            f"output: {usage.get('output_tokens', '?')}"
        )

        stop_reason = result.get("stop_reason")
        if stop_reason:
            if stop_reason == "max_tokens":
                print("WARNING: Response was truncated — increase max_tokens")
            if stop_reason != "end_turn":
                print(f"Stop reason: {stop_reason}")

        return result["content"][0]["text"]
