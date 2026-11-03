# Follow-ups (items blocked on external inputs)

- **Payment gateways:** JazzCash, Easypaisa and Stripe adapters need merchant credentials. The `PaymentGateway` interface, `FakeGateway`, signed idempotent webhook endpoint and `pay_at_venue` flow are done; online checkout (creating a payment during booking and moving the appointment on payment) is intentionally not wired.
- **SMS provider:** a real provider adapter skeleton is added in Step 11; credentials and sender ID are needed to enable it.
