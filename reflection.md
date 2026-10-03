# Customer Support AI Agent — Project Reflection

I built a customer-support agent with Amazon Bedrock AgentCore that connects reasoning with real backend services through Gateway/MCP. I also used AgentCore Memory, a Knowledge Base, Code Interpreter, and Browser to cover different parts of a customer-support workflow.

## Design Decision

One design decision I found useful was using Gateway/MCP as the integration layer instead of putting backend calls directly in the agent. This keeps the agent focused on understanding the customer's request while the Gateway handles how the request reaches a service. I used an API Gateway REST API for order lookups and a Lambda target for refund operations. The agent discovers these tools through the Gateway, so changing a backend does not require changing the agent's reasoning logic.

## Testing and Challenge

One thing I learned during testing was how important it is to validate the actual calculation behind an agent response. My first loyalty test applied the Gold discount but did not redeem the expected points. I traced this to the calculation mixing dollar values and loyalty points. After correcting the conversion and redeploying, the same test with 4,250 points and a $150 order redeemed 4,000 points for $40, applied an $11 Gold discount, and produced a $99 final total. That gave me confidence that the Code Interpreter path was working as intended.

## Production Consideration

For production, I would not keep the Gateway's `NONE` authorizer. Refunds especially would need authentication, authorization, audit logging, and possibly approval or transaction limits.
