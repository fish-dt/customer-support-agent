"""
Customer Support AI Agent — Starter Code
==========================================
AWS AgentCore customer-support agent with:

- AgentCore Runtime
- Amazon Bedrock Knowledge Base RAG
- AgentCore long-term memory
- AgentCore Code Interpreter
- AgentCore Browser
- AgentCore Gateway via MCP
- Gateway API and Lambda-backed tools
"""

# ── Imports ───────────────────────────────────────────────────────────────────
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict, Literal, Optional
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser
from pydantic import BaseModel, Field, ValidationError


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── 1 — App Initialisation ──────────────────────────────────────────────────────
app = BedrockAgentCoreApp()

# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── 2 — Configuration ────────────────────────────────────────────────────

REGION = os.getenv("AWS_REGION", "us-east-1")
GATEWAY_URL = os.getenv("GATEWAY_URL", "")
KB_ID = os.getenv("KB_ID", "")
MEMORY_ID = os.getenv("MEMORY_ID", "")

# ── 3 — Model and Clients ────────────────────────────────────────────────
model = "global.amazon.nova-2-lite-v1:0"
model = BedrockModel(model_id=model)
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── 4 — Namespace Helper ─────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    namespaces: Dict[str, str] = {}

    for strategy in strategies:
        strategy_type = strategy.get("type", "UNKNOWN")

        templates = strategy.get("namespaceTemplates")
        if templates:
            namespace = templates[0]
        else:
            # Fall back to the legacy AgentCore response shape.
            legacy = strategy.get("namespaces")
            if not legacy:
                continue
            namespace = legacy[0]

        namespaces[strategy_type] = namespace

    return namespaces

# ── 5 — Memory Hook ──────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id)

    @staticmethod
    def _extract_text(content) -> str:
        """Concatenate the text blocks of a Strands message content list."""
        if not content:
            return ""
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and "text" in block
        )

    @staticmethod
    def _is_tool_message(content) -> bool:
        """True if this message content contains a tool call/result, not plain text."""
        if not content:
            return False
        return any(
            isinstance(block, dict) and ("toolResult" in block or "toolUse" in block)
            for block in content
        )

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if not messages:
            return

        last_message = messages[-1]
        if last_message.get("role") != "user":
            return

        content = last_message.get("content", [])
        if self._is_tool_message(content):
            return

        query = self._extract_text(content).strip()
        if not query:
            return

        collected = []
        for strategy_type, namespace_template in self.namespaces.items():
            namespace = namespace_template.format(actorId=self.actor_id)
            try:
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace,
                    query=query,
                    top_k=5,
                )
            except Exception as e:
                logger.warning("Memory retrieval failed for namespace %s: %s", namespace, e)
                continue

            for mem in memories or []:
                text = ""
                if isinstance(mem, dict):
                    text = mem.get("content", {}).get("text", "")
                if text:
                    collected.append(f"[{strategy_type}] {text}")

        if collected:
            memory_block = "\n".join(collected)
            last_message["content"] = [
                {"text": f"Customer Context:\n{memory_block}\n\n{query}"}
            ]


    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        messages = event.agent.messages

        customer_query = None
        agent_response = None

        for message in reversed(messages):
            role = message.get("role")
            content = message.get("content", [])

            if self._is_tool_message(content):
                continue

            text = self._extract_text(content).strip()
            if not text:
                continue

            if role == "assistant" and agent_response is None:
                agent_response = text
            elif role == "user" and customer_query is None:
                customer_query = text

            if customer_query and agent_response:
                break

        if not (customer_query and agent_response):
            return

        try:
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")],
            )
        except Exception as e:
            logger.warning("Failed to save interaction to memory: %s", e)

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── 6 — Knowledge Base Tool ─────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID or KB_ID == "<kbid>":
        return "Knowledge base not configured."

    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
    except Exception as e:
        logger.error("Knowledge base retrieve() failed: %s", e)
        return f"Knowledge base search failed: {e}"

    results = resp.get("retrievalResults", [])
    if not results:
        return "No relevant information was found in the knowledge base for that query."

    chunks = [
        r.get("content", {}).get("text", "")
        for r in results
        if r.get("content", {}).get("text")
    ]
    if not chunks:
        return "No relevant information was found in the knowledge base for that query."

    return "\n---\n".join(chunks)


# ── Loyalty Discount — Pydantic Models ───────────────────────────────────────
# Structured-output validation: the sandbox only ever prints JSON, but nothing
# guarantees the LLM-influenced code path or the interpreter's own response
# envelope actually matches the shape we promise the agent. Validating both
# the input and the computed result against explicit models means a malformed
# value is caught here and reported clearly, instead of reaching the customer
# as a silently wrong discount.

class LoyaltyDiscountInput(BaseModel):
    """Validated input for a loyalty discount calculation."""
    loyalty_points: int = Field(ge=0, description="Customer's current points balance")
    tier: Literal["Silver", "Gold", "Platinum"] = Field(
        description="Customer loyalty tier"
    )
    order_total: float = Field(gt=0, description="Order total in USD")
    product_category: Literal["standard", "device", "fresh"] = Field(
        default="standard", description="Category used to determine the points earn rate"
    )


class LoyaltyDiscountResult(BaseModel):
    """Validated discount breakdown returned to the agent."""
    points_redeemed: int = Field(ge=0)
    points_redeemed_value: float = Field(default=0.0, ge=0)
    tier: str
    tier_discount_pct: float = Field(ge=0, le=100)
    tier_discount_amount: float = Field(ge=0)
    order_total: float = Field(gt=0)
    final_total: float = Field(ge=0)
    total_savings: float = Field(default=0.0, ge=0)
    points_earned: int = Field(default=0, ge=0)
    remaining_points: int = Field(ge=0)
    note: Optional[str] = None


# ── 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price, validated against
        LoyaltyDiscountResult.
    """
    # Validate inputs before touching the sandbox or the Lambda-priced tools.
    try:
        validated_input = LoyaltyDiscountInput(
            loyalty_points=loyalty_points,
            tier=tier,
            order_total=order_total,
            product_category=product_category,
        )
    except ValidationError as e:
        logger.error("Loyalty discount input validation failed: %s", e)
        return json.dumps({"error": "Invalid discount parameters", "details": str(e)})

    loyalty_points = validated_input.loyalty_points
    tier = validated_input.tier
    order_total = validated_input.order_total
    product_category = validated_input.product_category

    # 100 points = $1 (1 point = $0.01), per the loyalty program reference.
    # Redemption happens in blocks of 500 points (the program minimum),
    # capped at 50% of the order total so a customer can never zero out an order.
    code = f"""
import json

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

loyalty_points = {loyalty_points}
tier = "{tier}"
order_total = {order_total}
product_category = "{product_category}"

max_redeemable_value = order_total * 0.5
points_value = loyalty_points * 0.01
redeemable_value = min(points_value, max_redeemable_value)

max_redeemable_points = int(max_redeemable_value * 100)
points_redeemed = min(
    (loyalty_points // 500) * 500,
    (max_redeemable_points // 500) * 500,
)
redeemed_value = points_redeemed * 0.01

subtotal_after_points = order_total - redeemed_value

tier_discount_rate = tier_rates.get(tier, 0.0)
tier_discount_amount = round(subtotal_after_points * tier_discount_rate, 2)

final_total = round(subtotal_after_points - tier_discount_amount, 2)
total_savings = round(order_total - final_total, 2)

earn_rate = earn_rates.get(product_category, 1)
points_earned = int(final_total * earn_rate)
remaining_points = loyalty_points - points_redeemed + points_earned

result = {{
    "points_redeemed": points_redeemed,
    "points_redeemed_value": round(redeemed_value, 2),
    "tier": tier,
    "tier_discount_pct": round(tier_discount_rate * 100, 2),
    "tier_discount_amount": tier_discount_amount,
    "order_total": order_total,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
}}

print(json.dumps(result))
"""

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    "clearContext": True,
                },
            )
            for event in response.get("stream", []):
                result_event = event.get("result")
                if not result_event:
                    continue

                # The sandbox's response envelope wraps our printed stdout as
                # one or more {"type": "text", "text": ...} content blocks —
                # it is not the discount dict itself. Pull the JSON we
                # printed out of it before validating.
                stdout_text = "".join(
                    block.get("text", "")
                    for block in result_event.get("content", [])
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()

                try:
                    parsed = json.loads(stdout_text)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.error("Could not parse code interpreter stdout as JSON: %s", e)
                    return json.dumps({
                        "error": "Code interpreter did not return valid JSON.",
                        "raw_output": stdout_text,
                    })

                try:
                    validated_result = LoyaltyDiscountResult.model_validate(parsed)
                except ValidationError as e:
                    logger.error("Code interpreter output failed schema validation: %s", e)
                    return json.dumps({
                        "error": "Discount calculation returned an unexpected shape.",
                        "details": str(e),
                        "raw_output": parsed,
                    })

                return validated_result.model_dump_json()

        return json.dumps({"error": "No result returned from the code interpreter."})

    except Exception as e:
        logger.warning("Code interpreter unavailable, using fallback calculation: %s", e)
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_discount_rate = tier_rates.get(tier, 0.0)
        tier_discount_amount = round(order_total * tier_discount_rate, 2)
        final_total = round(order_total - tier_discount_amount, 2)

        try:
            fallback_result = LoyaltyDiscountResult(
                points_redeemed=0,
                points_redeemed_value=0.0,
                tier=tier,
                tier_discount_pct=round(tier_discount_rate * 100, 2),
                tier_discount_amount=tier_discount_amount,
                order_total=order_total,
                final_total=final_total,
                total_savings=tier_discount_amount,
                points_earned=0,
                remaining_points=loyalty_points,
                note=(
                    "Fallback calculation used — Code Interpreter was unavailable. "
                    "Points redemption was not applied; only the tier discount was calculated."
                ),
            )
        except ValidationError as ve:
            # Should not happen since we control every field here, but we should never
            # let a validation bug surface as an unhandled exception.
            logger.error("Fallback discount result failed validation: %s", ve)
            return json.dumps({"error": "Unable to compute a valid discount fallback."})

        return fallback_result.model_dump_json()


# ── 8 — Agent Entrypoint ─────────────────────────────────────────────────
@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        user_input = payload.get("prompt", "")
        actor_id = payload.get("customer_id", "anonymous")
        session_id = payload.get("session_id") or str(uuid.uuid4())

        
        hooks = []
        try:
            memory_hook = MemoryHook(
                actor_id=actor_id,
                session_id=session_id,
                memory_client=memory_client,
                memory_id=MEMORY_ID,
            )
            hooks = [memory_hook]
        except Exception as e:
            logger.warning(
                "MemoryHook unavailable, continuing without cross-session memory: %s", e
            )

        agent_core_browser = AgentCoreBrowser(region=REGION)

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        system_prompt = (
            "You are a helpful, professional customer support assistant for an "
            "e-commerce platform.\n\n"
            "TOOLS AVAILABLE THROUGH THE GATEWAY\n"
            "- get_order(order_id): look up a single order's status, items, "
            "tracking number, and carrier.\n"
            "- get_customer_orders(customer_id): list all orders for a customer.\n"
            "- get_customer(customer_id): look up a customer's profile, loyalty "
            "tier, and points balance.\n"
            "- initiate_refund(order_id, reason, amount): start and approve a "
            "refund for an order.\n"
            "- check_refund_status(refund_id): check an existing refund's status.\n"
            "- get_return_label(order_id): generate a prepaid return shipping "
            "label.\n\n"
            "OTHER TOOLS\n"
            "- search_knowledge_base(query): product specs, return/refund "
            "policy windows, warranty terms, loyalty tier benefits, and order "
            "status definitions.\n"
            "- calculate_loyalty_discount(...): exact points-redemption and "
            "tier-discount math for an order — always use this instead of "
            "computing discounts yourself.\n"
            "- browser: fetch live content from a web page when asked.\n\n"
            "GUIDELINES\n"
            "Always confirm order or customer IDs before acting on them "
            "(especially before initiating a refund). Be concise. Never "
            "fabricate order, refund, or discount details — call the "
            "appropriate tool to get exact figures instead of guessing."
        )

        mcp_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
        with mcp_client:
            gateway_tools = mcp_client.list_tools_sync()
            tools.extend(gateway_tools)

            agent = Agent(
                model=model,
                tools=tools,
                hooks=hooks,
                system_prompt=system_prompt,
            )

            response = agent(user_input)

        content = response.message.get("content", [])
        for block in content:
            if isinstance(block, dict) and "text" in block:
                return block["text"]

        return str(response)

    except Exception as e:
        logger.error("Agent invocation failed: %s", e)
        return (
            "I'm sorry, something went wrong while processing your request. "
            f"Details: {e}"
        )


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
