# Web dashboard design guide

For agents and contributors building or changing a screen of the browser app.
The decisions behind it are in [ADR 0007](../docs/adr/0007-web-dashboard-design-system.md):
Material 3 with tonal surfaces and no shadows, one blue seed, Reka UI as a
headless behaviour library only inside the primitives, Inter and Material
Symbols bundled. The `/ui` gallery (logged in, linked from no menu) shows every
token and primitive under each theme and display profile. It is the visual
reference.

The rules in short:

- Build screens from the primitives below. A view never imports `reka-ui`.
- Style with `--md-*` tokens only. Stylelint rejects raw colours, sizes,
  radii, z-indexes, spacing, shadows and durations outside `tokens.css`.
- On a primitive's root, a view may set layout only (margin, grid placement,
  flex), never appearance (colour, border, opacity, type weight). Add a primitive
  prop and a gallery sample instead; never override primitive internals.
- A theme or profile difference is a token, or a rule scoped without `:root`.
- Mockups in design sessions use the real token names and the real
  primitives' class names, so the look reaches the implementer.

## Which primitive for which job

All live in `src/components/ui/`.

| Job | Primitive | Notes |
|---|---|---|
| A collapsible tool summary | `Disclosure` | `label`, `error`, `v-model:open`; bundled chevron, monospace summary and tonal surface. |
| A transcript entry | `TranscriptEntry` | `kind` user/assistant/thinking/orphan and `meta`; user prompts use primary-container, meta uses a quiet hairline. Sanitised Markdown remains document content, not primitive internals. |
| A command with a text label | `Button` | `variant`: `primary` (one per screen), `tonal`, `text`, `danger`. Optional leading `icon`. `type="submit"` inside a form. |
| A command shown only as an icon | `IconButton` | `label` is required and becomes the accessible name. The hit target is `--md-target-min`. `variant="text"` is transparent; `variant="tonal"` gives floating controls an opaque surface and an e-ink hairline. |
| An on/off setting | `Switch` | `v-model` boolean, `label` required. Not for a one-off action: use a `Button`. |
| A container, phase or agent state | `StatusChip` | Never map wire values by hand: `containerStatus`, `agentStatus` and `rowStatuses` in `components/ui/status.ts` turn wire values into chips. Use `listStatuses(row)` for list policy: phase first, otherwise stopped only (no Running/Frozen chip), then agent and emphasized recent-idle age. Use the mapper's label casing everywhere. `emphasis` owns strong weight. An unknown value becomes a neutral chip with its raw text. |
| A row of a list (container, repo, setting) | `ListRow` | `title`/`secondary` props or slots, a default body slot, a `trailing` slot for controls. `to` makes the whole row a route link; controls in the body and trailing slots stay their own targets. Height follows `--md-target-row`. `context` gives supporting ancestors reduced prominence, or a dashed hairline in e-ink. `wrap` keeps long names readable and trailing chips above the body without view overrides of primitive internals. |
| A glyph | `Icon` | `name` from `material-symbols/paths.ts`, `size` small/medium/large. Takes `currentColor`. Without `label` it is decorative (`aria-hidden`). |
| A modal question or form, or a side drawer | `Dialog` | `title` required, `description` optional, `placement="drawer"` for a side panel. Body in the default slot; buttons in the `#actions` slot, whose `close` slot prop closes it. A trigger in `#trigger`, or drive `v-model:open`. |
| A list of actions behind a button | `Menu` | `label`, `items` (`MenuItem`: `value`, `label`, optional `icon`, `disabled`, `variant: "danger"`), `select` event with the value. The trigger goes in `#trigger`. |
| A text input | `Field` | `v-model`, `label`, `help`, `error`, `type`. Error text is announced and wires `aria-invalid`. |
| A choice from a fixed list | `Select` | `v-model`, `label`, `options` (`SelectOption`), `placeholder`, `help`, `error`. |
| A label, help and error around another control | `FieldFrame` | The frame `Field` and `Select` are built on. Bind the default slot's `controlAttrs(attrs)` onto the control; it sets the id, `aria-labelledby`, `aria-describedby` and `aria-invalid`. Never hand-roll label/help/error markup. |

`Button` and `IconButton` also accept `compact` for the transcript reader's
26px bar / 28px indicator. Drawn chrome is compact, but a pseudo-element extends
the hit area upwards to `--md-target-min`, never into the bottom guard. Leave
that area clear of other targets; do not use this density for ordinary forms.
The Reader gallery group shows these controls and the transcript primitives.

Shared helper classes are prefixed `ui-` so no view class collides with them:
`ui-state-layer` (Material 3 hover/pressed overlay) and `ui-focus-ring` in
`state-layer.css`; `ui-field`, `ui-field-label`, `ui-field-help`,
`ui-field-error`, `ui-input` in `fields.css`; `ui-dialog`, `ui-dialog-scrim`,
`ui-popup`, `ui-option`, `ui-overlay-scope` in `overlays.css`. Option rows of
a menu or select are `--md-target-row` high. Portalled overlays carry the
nearest `data-theme`/`data-profile` with them (`overlayScope.ts`), so a dialog
opened from a gallery panel keeps that panel's look.

## Token families

All tokens are defined in `src/styles/tokens.css`, which holds only custom
property declarations and `@font-face`; every other rule goes in `base.css` or
a component. The names are Material 3 roles under the `md` prefix.

| Family | Tokens | Use |
|---|---|---|
| Surfaces | `--md-surface`, `--md-surface-container*` (`-lowest`, `-low`, plain, `-high`, `-highest`) | Page and card backgrounds; tonal elevation instead of shadows. |
| Content on surfaces | `--md-on-surface`, `--md-on-surface-variant` | Text and icons; variant for secondary text. |
| Outlines | `--md-outline`, `--md-outline-variant` | Strong and quiet borders, dividers. |
| E-ink hairline | `--md-hairline` | A border only the e-ink theme draws (transparent in light and dark), where e-ink has no tints to tell surfaces apart. |
| Scrim | `--md-scrim` | The dimming behind a modal: transparent in the e-ink theme, and overlays in the e-ink profile drop it too. |
| Primary | `--md-primary`, `--md-on-primary`, `--md-primary-container`, `--md-on-primary-container` | Accent, primary buttons, selection, notices. |
| Error | `--md-error`, `--md-on-error`, `--md-error-container`, `--md-on-error-container` | Failures, danger buttons, field errors. |
| Phase colours | `--md-phase-*`, `--md-on-phase-*` (booting, autostart, job, failed: a colour, a `-container`, and `--md-on-phase-…-container` on it) | The terminal dashboard's phase hues. Reach them through `StatusChip`. |
| Fonts | `--md-font-body`, `--md-font-mono` | Inter, and the platform monospace stack. |
| Type scale | `--md-type-*` (`title`/`body`/`label` × `large`/`medium`/`small`, each `-size`, `-line-height`, `-weight`) | Every font size. In rem, so the body-size setting scales them. |
| Icon sizes | `--md-icon-*` (`small`, `medium`, `large`) | Via `Icon`'s `size`. |
| Shape | `--md-shape-*` (`none`, `small` 8px, `medium` 12px, `large` 16px, `extra-large` 28px, `full`) | Every border radius. |
| Spacing | `--md-space-*` (`4`, `8`, `12`, `16`, `24`, `32`) | Every margin, padding, gap and inset. See the naming rule below. |
| Hit targets | `--md-target-min`, `--md-target-row` | `min` 44px: the smallest any control may be. `row` 48px with a fine pointer, 56px with a coarse one. |
| Layers | `--md-layer-*` (bottom to top: `raised`, `sticky`, `scrim`, `dialog`, `popover`) | Every z-index. A menu or select opened from a dialog sits above it. |
| State layers | `--md-state-*` (`hover-opacity`, `pressed-opacity`) | Through `ui-state-layer`; zero in e-ink. |
| Emphasis | `--md-type-emphasis-weight` | Strong weight for meaningful supporting status, such as recent activity; owned by `StatusChip`'s `emphasis` prop. |
| Context | `--md-context-opacity` | Reduced visual prominence for supporting context rather than primary results. `ListRow`'s `context` prop owns this treatment; e-ink keeps full black ink and uses a dashed hairline instead of grey. |
| Disabled | `--md-disabled-*` (`content-opacity`, `container-opacity`) | Material 3 fading; none in e-ink (no greys). |
| Focus ring | `--md-focus-ring-*` (`width`, `offset`) | Through `ui-focus-ring`; kept in every theme. |
| Motion | `--md-motion-*` (`duration`, `easing`) | Every transition. `--md-motion-duration` is 150ms in the normal profile and 0ms in the e-ink profile and under reduced motion. |
| Switch geometry | `--md-switch-*` (`track-width`, `track-height`, `handle-inset`) | `Switch` only. |

The old names (`--bg`, `--text`, `--accent`, `--space-1`…`--space-4`,
`--radius`, …) are aliases of these until ticket 14 of #167 removes them. New
code uses only `--md-*`. App metrics at the end of `tokens.css`
(`--title-bar-height`, `--guard-bottom`, …) are layout values set by code,
not design tokens.

### Spacing naming rule

A spacing token is named by its px size at the default 16px root:
`--md-space-4` 0.25rem, `--md-space-8` 0.5rem, `--md-space-12` 0.75rem,
`--md-space-16` 1rem, `--md-space-24` 1.5rem, `--md-space-32` 2rem. The old
aliases map by value:

| Old | New |
|---|---|
| `--space-1` | `--md-space-4` |
| `--space-2` | `--md-space-8` |
| `--space-3` | `--md-space-16` |
| `--space-4` | `--md-space-24` |

Migrate through this table, never by index: `--space-3` becomes
`--md-space-16`, not `--md-space-3`, which does not exist. `tokens.test.ts`
fails on any `--md-*` name the sources use that `tokens.css` does not define.

## Themes and the e-ink display profile

The theme (`light`, `dark`, `eink`) sets colours; the display profile
(`normal`, `eink`) sets behaviour. Each is chosen on its own: light theme with
the e-ink profile is valid.

The e-ink theme is black on white: no greys for text, no tints, no state
layers, `--md-hairline` borders, underlined links. Shape, type, spacing and hit targets
carry over unchanged. The e-ink profile never animates (`--md-motion-duration` is
0ms), turns pages instead of scrolling, and has no dimming backdrops: the
overlay primitives drop the scrim and draw a hairline border instead. No
component draws a shadow in any theme. Under `prefers-reduced-motion` nothing moves either; `base.css`
stops every transition.

### Scoping convention

Custom properties inherit from the nearest `[data-theme]` / `[data-profile]`
element, so a gallery panel shows its own theme and profile whatever the
page's. Therefore:

1. Express a theme or profile difference as a token in `tokens.css` first.
2. Failing that, scope the rule without `:root`:
   `[data-profile="eink"] .x { … }` or `[data-theme="eink"] .x { … }`.
3. Never write `:root[data-…] .x`. No gallery panel can scope it, and a root
   e-ink rule leaks into the normal-profile panels.

`:root` is only for once-per-page things (body padding, page controls). The
e2e test `test_gallery_panels_follow_their_own_theme_and_profile_not_the_page`
checks the gallery under light/normal, e-ink/e-ink and dark/e-ink pages.

## Adding or changing a primitive

1. **Tokens only.** Write the component in `src/components/ui/` with values
   from `tokens.css`. If the design needs a value no token has, add a
   design-scale token to `tokens.css` (with its light, dark and e-ink values
   where it differs), not a one-off value or a private custom property:
   Stylelint rejects a custom property that introduces raw numbers outside
   `tokens.css`. Keep `tokens.css` to design-scale values; component geometry
   stays in the component. Behaviour from Reka UI is imported only here.
   Icons take `currentColor`; a new icon is added to `material-symbols/paths.ts`
   and the table in `material-symbols/NOTICE.md`.
2. **Gallery entry.** Show every variant and state in a `.group` with a
   `.group-title` in `src/views/GalleryView.vue`, inside the per-panel loop, so
   it renders under every theme and profile.
3. **Vitest.** Test through the component's public surface (props, slots,
   events, roles and accessible names) with Vue Test Utils (`mount`), beside the
   existing ones (`primitives.test.ts`, `overlays.test.ts`, `status.test.ts`).
   Add browser behaviour that jsdom cannot show (real layout, focus, portals)
   to `tests_e2e/`.
4. **Snapshots.** Regenerate the gallery references and review the PNG diff
   before committing it with the change:

   ```bash
   make web-e2e-snapshots   # rewrites only the groups that changed
   make web-e2e             # verify without updating
   ```

   Details, budgets and failure evidence: "Gallery screenshot references" in
   `tests_e2e/README.md`. Do not loosen the budgets to accept a change.

## Running the checks

```bash
make web-test                         # npm ci, Stylelint, then Vitest
cd frontend && npm run lint:styles    # Stylelint alone
cd frontend && npm test               # Vitest alone
make web-e2e                          # build, then the Chrome suite incl. snapshot comparison
```

`stylelint.config.mjs` lists the views not yet migrated in `ignoreFiles`. A
migration ticket removes its own entries; never add a file there to get past
the linter. The rule itself is `stylelint/design-tokens.mjs`.

## Mockups

Mockups made in design sessions (visual companion, artifacts) use the real
token names from `tokens.css` and the real `ui-` class names, not invented
ones, and copy the token values instead of approximating them. A mockup's
look then reaches the implementer through names the code already has; a look
that lives only in mockup CSS is lost.
