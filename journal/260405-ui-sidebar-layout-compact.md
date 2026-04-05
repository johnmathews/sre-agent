# Streamlit sidebar layout: compact health + reordered sections

## Problem

In the Streamlit sidebar, **Infrastructure Health** was rendered below the
**Past conversations** list. Since the conversation list can easily be 5–20
items tall, the health panel was almost always off-screen — you had to scroll
past every past conversation to see whether the backend was even reachable.

The conversation title cards also wasted horizontal space:

- Button padding was Streamlit's default — tall, chunky rows.
- The `⋯` overflow menu always occupied a full column (ratio `[5, 1]`, ~17%
  of the sidebar width), even though users interact with it rarely.

## Changes

### Reordered sidebar sections

New order in `src/ui/app.py`:

1. Title ("SRE Assistant")
2. "+ New conversation" button
3. Session id caption
4. **Infrastructure Health** (moved up)
5. **Past conversations**

Health now stays visible without scrolling.

### Compact health display

Replaced the multi-line `st.success` / `st.warning` / `st.error` box +
per-component `st.markdown` lines with:

- A single inline line: `**Health** ● degraded (5/6)` — status word plus
  a colored dot (`:green[●]` / `:orange[●]` / `:red[●]`) and a healthy/total
  count.
- A collapsed `st.expander("Details")` containing the LLM model caption and
  the per-component list. The expander auto-opens only when status is
  non-healthy, so degraded/unhealthy states are still visible on first render.

### Compact conversation rows

- Column ratio changed from `[5, 1]` → `[10, 1]` with `gap="small"`, giving
  titles ~91% of the row width instead of ~83%.
- CSS injected at the top of the sidebar reduces button padding/font in the
  whole sidebar: `padding: 0.3rem 0.6rem`, `font-size: 0.85rem`,
  `min-height: 0`, `line-height: 1.25`.

### Hover-reveal `⋯` button

The overflow menu button is hidden by default (`opacity: 0`, 0.15s fade)
and appears only when its row is hovered. The CSS selector:

```css
section[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"]
    > div[data-testid="stColumn"]:nth-child(2):nth-last-child(1)
    div[data-testid="stButton"] > button { opacity: 0; … }
```

The `:nth-child(2):nth-last-child(1)` pair matches the 2nd column **only
when it is also the last child** — i.e., a 2-column row. This deliberately
excludes the 3-column Save/Delete/Cancel action row (which appears below a
conversation when renaming), so those buttons stay visible.

## Why CSS injection rather than `st.popover`

`st.popover` would avoid the toggle+expansion dance entirely and collapse the
rename UI into a floating popup. It was tempting, but:

1. The existing rename flow (inline text_input + Save/Delete/Cancel row) is
   already built and tested — switching to popover would be a UX rework, not
   a layout tweak.
2. The user's request was specifically about visible space, not interaction
   model — hover-reveal directly addresses "doesn't need to take up so much
   space" while keeping behavior identical.

## Files touched

- `src/ui/app.py` — sidebar reorder, CSS block, compact health, column ratio
- `docs/conversation-history.md` — updated "UI Sidebar" section to describe
  hover-reveal and the new layout order
