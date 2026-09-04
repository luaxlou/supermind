# Product state

Product state is the smallest durable description needed to evolve the product coherently.

- **Baseline**: What the product currently promises and demonstrably does.
- **Scenario**: A user's context, intent, actions, and observable result.
- **Feature**: A coherent user-visible capability that may participate in several scenarios.
- **Behavior contract**: Inputs, rules, outputs, boundaries, and visible failure behavior.
- **Capability**: A reusable implementation or service with a stable contract, owner, version, and
  known consumers.
- **Evidence**: Observable results that support a claimed product behavior or quality.

For a new product, establish one valuable end-to-end scenario as the initial spine. This does not
make every later change an end-to-end redesign.

For existing products, locate the affected state first. Update only the scenarios, features,
contracts, capabilities, and evidence changed by the work. A local implementation change may require
no durable product-document update.

Prefer existing repository locations and formats. When no durable product documentation exists and
the new understanding will be needed by later work, use concise files under `docs/product/`, such as
`baseline.md`, `features/<name>.md`, or `capabilities/<name>.md`. Create only the files the product
actually needs.

Keep design and implementation plans with the task method that created them. Promote their product
decisions into product state; do not copy the whole task record.

