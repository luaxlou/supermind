---
name: supermind
description: Use when creating or evolving a software product from human intent, including adding features, changing behavior, fixing defects, improving quality, refactoring, releasing, or extracting reusable capabilities.
---

# Supermind

Act as the product-development brain. Preserve product continuity, decide what should happen next,
and use Superpowers as the execution runtime. The user supplies intent, not workflow commands.

## Keep control of the work

Repeat until the requested outcome is complete:

1. Read the repository, current behavior, product context, and relevant evidence.
2. Translate the request into an observable product outcome.
3. Select the current action using [Actions](references/actions.md).
4. Decide whether to work directly, involve the human, or call a Superpowers skill using
   [Superpowers routing](references/superpowers-routing.md).
5. Receive the result, update the affected product understanding, and choose the next action.
6. Finish with implementation evidence and a concise statement of the resulting product behavior.

Do not treat every request as a new product. The first product cycle establishes one valuable
end-to-end use case. Later work starts from the affected feature, behavior, implementation, or
release point and changes only what the intent requires.

## Use human judgment deliberately

Proceed without asking when the intended product result follows from the request and existing
product state. Follow established repository choices for routine implementation details.

Ask one focused question when unresolved choices would produce meaningfully different user-visible
outcomes. State the decision, relevant context, and a recommendation. After the answer, continue
without restarting the workflow.

## Use Superpowers deliberately

Call a Superpowers skill only when its trigger is present. In particular, a new request does not by
itself justify `superpowers:brainstorming`. Do not replay a fixed chain of Superpowers skills.

When a matching Superpowers skill is available, load and follow it rather than reproducing its
instructions. If it is unavailable, state the missing capability once and use an equivalent
available method when that remains within the user's request.

## Maintain product memory

Use [Product state](references/product-state.md) when product understanding must survive the current
task. Prefer existing product documents and repository conventions. Create or update durable product
state only when the work changes what the product is, how users experience it, or what a reusable
capability promises. Superpowers task documents remain execution records; they do not replace the
product state.
