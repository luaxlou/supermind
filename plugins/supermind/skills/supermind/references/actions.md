# Actions

Actions are internal decisions, not commands the user must choose.

- **Create**: No usable product baseline exists. Establish the smallest valuable end-to-end use case,
  make it work, and use it as the first product baseline.
- **Add**: Introduce a new user-visible capability. Connect it to existing scenarios and features;
  add a new end-to-end scenario only when the capability creates one.
- **Change**: Move intended behavior from A to B. Update every affected scenario, feature contract,
  implementation, and verification result. B becomes the new baseline.
- **Fix**: Restore behavior that does not match the existing intent or contract. Do not redefine the
  product merely to match the defect.
- **Improve**: Make a measurable quality better while preserving intended behavior—for example speed,
  usability, reliability, accessibility, or maintainability.
- **Refactor**: Change internal structure while preserving externally observable behavior and contracts.
- **Release**: Turn completed product changes into an identifiable, verified delivery.
- **Reuse**: Reuse an existing capability or extract a proven capability whose contract, ownership,
  version, and consumers can stand apart from one product.

Migration is not a standalone action. Treat it as implementation work attached to Create, Add,
Change, Improve, Refactor, Release, or Reuse.

One request may require several actions. Select only the next action that advances the outcome, then
reassess after its result.

