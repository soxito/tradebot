---
name: ui-ux-pro-max
description: "UI/UX design intelligence for CivicCollect web surfaces. Use for dashboards, admin panels, landing pages, forms, tables, charts, navigation, responsive layouts, accessibility, motion, dark mode, loading and empty states, typography, color systems, and premium frontend polish. DO NOT USE FOR: pure backend logic, API or database-only work, infrastructure, or non-visual automation tasks."
argument-hint: "Describe the UI/UX task — e.g. 'redesign the dashboard', 'review this page for accessibility', 'choose a premium visual direction for the collections portal'"
---

# UI/UX Pro Max

Adapted from the upstream project at `https://github.com/nextlevelbuilder/ui-ux-pro-max-skill`.

This local mirror gives CivicCollect agents a shared UI/UX decision model without depending on the external installer or CLI assets at runtime.

## When To Use

### Must Use

- Designing or redesigning pages, dashboards, admin panels, public portals, drawers, forms, tables, cards, charts, or navigation.
- Changing visual direction, typography, spacing, color systems, iconography, motion, hierarchy, or perceived quality.
- Reviewing UI for usability, accessibility, responsiveness, dark mode, or state clarity.
- Fixing interface bugs that affect layout shift, touch targets, loading feedback, empty states, visual hierarchy, or interaction clarity.

### Recommended

- The UI feels unfinished, inconsistent, or not premium enough and the problem is not obvious.
- Cross-page design consistency is drifting.
- A frontend change needs a fast design QA pass before delivery.

### Skip

- Pure backend logic, services, policies, migrations, or queue orchestration.
- API or database design work that does not change the interface.
- Infrastructure, DevOps, or deployment-only tasks.
- Non-visual scripts or automation.

Decision rule: if the task changes how a feature looks, feels, moves, or is interacted with, use this skill.

## Working Sequence

1. Clarify the product surface: audience, page type, data density, primary task, and success state.
2. Pick one coherent design direction before editing: style, typography, palette, spacing density, and motion posture.
3. Review or implement the interface in this priority order:
   - Accessibility
   - Touch and interaction
   - Performance
   - Style consistency
   - Layout and responsiveness
   - Typography and color
   - Animation
   - Forms and feedback
   - Navigation
   - Charts and data
4. Re-check the finished UI against the delivery checklist before stopping.

## Priority Checks

### 1. Accessibility

- Maintain at least 4.5:1 contrast for normal text and 3:1 for large UI glyphs.
- Preserve visible focus states on every interactive control.
- Use real labels for fields and `aria-label` for icon-only actions.
- Support keyboard navigation and logical focus order.
- Respect reduced-motion preferences and avoid motion-only meaning.

### 2. Touch And Interaction

- Keep primary touch targets at least 44x44 pixels.
- Leave enough spacing between adjacent actions to prevent mis-taps.
- Provide visible press and loading feedback quickly.
- Do not rely on hover-only interactions for critical behavior.
- Keep destructive actions visually distinct and explicitly confirmed when needed.

### 3. Performance

- Reserve space for async content to avoid layout shift.
- Prefer skeletons or progressive placeholders for content that takes noticeable time.
- Lazy load heavy views, charts, modals, and below-the-fold media.
- Debounce high-frequency input, filter, and resize work.
- Avoid animation patterns that trigger layout thrashing.

### 4. Style Consistency

- Choose one visual language per surface and keep it consistent across cards, forms, tables, and navigation.
- Use one icon family and never use emoji as structural icons.
- Keep one clear primary action per screen section.
- Match shadows, radii, and density across the page.

### 5. Layout And Responsiveness

- Design mobile-first and prevent horizontal scroll.
- Preserve comfortable spacing using a consistent 4px or 8px rhythm.
- Keep core content visible first on small screens and move secondary details behind disclosure when needed.
- Ensure sticky or fixed bars do not hide content.
- Preserve readable content width on larger screens.

### 6. Typography And Color

- Keep mobile body text at 16px or larger where practical.
- Use semantic color roles instead of random ad-hoc values.
- Keep hierarchy obvious through size, weight, spacing, and contrast.
- Test dark mode independently instead of assuming light-mode values translate.
- Prefer token-driven color and spacing choices when the project already has them.

### 7. Animation

- Keep micro-interactions in roughly the 150ms to 300ms range.
- Animate with transform and opacity instead of width, height, top, or left.
- Make motion purposeful and tied to state change or spatial continuity.
- Keep animations interruptible and never block input during them.

### 8. Forms And Feedback

- Show labels, helper text, field-local errors, and clear recovery paths.
- Give submit buttons loading feedback and confirm success or failure clearly.
- Use helpful empty states with an action, not blank space.
- Keep toast or async feedback accessible and non-disruptive.

### 9. Navigation

- Keep active state, current location, and back behavior predictable.
- Preserve scroll, filters, and other user context when moving between adjacent views.
- Do not use modals as the primary navigation model.
- Keep destructive or account-exit actions separated from normal navigation items.

### 10. Charts And Data

- Match the chart type to the question being answered.
- Provide legends, labels, and accessible color contrast.
- Support empty, loading, and error states for every chart surface.
- Prefer direct labeling or data tables when a chart would hide precise values.

## CivicCollect Integration Notes

- Stack assumptions: Vue 3, TypeScript, Inertia.js, Tailwind CSS, Laravel server-rendered shells.
- Reuse the existing UI primitives under `resources/js/Components/UI/` before inventing new base components.
- Keep pages lightweight and move repeated reactive logic into composables.
- Favor intentional, premium admin SaaS design over generic defaults.
- After frontend edits, run `npm run build 2>&1` before delivery.

## Delivery Checklist

- The interface handles loading, empty, error, and success states.
- Focus order, labels, and contrast are still correct after the change.
- Mobile and desktop layouts both remain usable.
- Motion is subtle, purposeful, and reduced-motion safe.
- No structural emoji icons, inconsistent spacing, or ad-hoc visual styles slipped in.
