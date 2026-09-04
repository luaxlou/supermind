# Superpowers routing

Supermind owns product direction and continuity. Superpowers supplies task methods. Route from the
current need rather than from the size or label of the request.

## Route by need

- Use `superpowers:brainstorming` when product intent, feature boundaries, or competing user-visible
  outcomes require a decision.
- Use `superpowers:writing-plans` when the target behavior is clear and coordinated implementation
  steps must be defined.
- Use `superpowers:systematic-debugging` when observed behavior has an unknown cause.
- Use `superpowers:executing-plans` or `superpowers:subagent-driven-development` when an approved plan
  is ready for execution and the selected method fits the environment.
- Use `superpowers:requesting-code-review` when implementation is ready for independent review.
- Use `superpowers:verification-before-completion` before claiming that the requested result works.
- Use `superpowers:finishing-a-development-branch` when completed repository work needs integration or
  handoff.

Use other available Superpowers skills when their own triggers match the task.

## Skip unnecessary routing

Work directly when the outcome, affected behavior, and implementation path are already clear and the
change is local. Examples include a precise copy change, an established configuration adjustment, or
a small addition inside an existing feature contract.

Do not use Brainstorming merely because work is new, visible, or called a feature. Technical
uncertainty calls for inspection, planning, or debugging; it is not automatically a product decision.

## Return control to Supermind

After every delegated task, reassess the product outcome. Absorb relevant results into product state,
decide whether more work is needed, and route again only when a new trigger appears.

