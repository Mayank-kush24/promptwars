---
name: Atmosphere
colors:
  surface: '#fdf7ff'
  surface-dim: '#ded8e0'
  surface-bright: '#fdf7ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f8f2fa'
  surface-container: '#f2ecf4'
  surface-container-high: '#ece6ee'
  surface-container-highest: '#e6e0e9'
  on-surface: '#1d1b20'
  on-surface-variant: '#494551'
  inverse-surface: '#322f35'
  inverse-on-surface: '#f5eff7'
  outline: '#7a7582'
  outline-variant: '#cbc4d2'
  surface-tint: '#6750a4'
  primary: '#4f378a'
  on-primary: '#ffffff'
  primary-container: '#6750a4'
  on-primary-container: '#e0d2ff'
  inverse-primary: '#cfbcff'
  secondary: '#63597c'
  on-secondary: '#ffffff'
  secondary-container: '#e1d4fd'
  on-secondary-container: '#645a7d'
  tertiary: '#765b00'
  on-tertiary: '#ffffff'
  tertiary-container: '#c9a74d'
  on-tertiary-container: '#503d00'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#e9ddff'
  primary-fixed-dim: '#cfbcff'
  on-primary-fixed: '#22005d'
  on-primary-fixed-variant: '#4f378a'
  secondary-fixed: '#e9ddff'
  secondary-fixed-dim: '#cdc0e9'
  on-secondary-fixed: '#1f1635'
  on-secondary-fixed-variant: '#4b4263'
  tertiary-fixed: '#ffdf93'
  tertiary-fixed-dim: '#e7c365'
  on-tertiary-fixed: '#241a00'
  on-tertiary-fixed-variant: '#594400'
  background: '#fdf7ff'
  on-background: '#1d1b20'
  surface-variant: '#e6e0e9'
typography:
  display:
    fontFamily: Google Sans
    fontSize: 48px
    fontWeight: '700'
    lineHeight: 56px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Google Sans
    fontSize: 32px
    fontWeight: '500'
    lineHeight: 40px
    letterSpacing: -0.01em
  headline-md:
    fontFamily: Google Sans
    fontSize: 24px
    fontWeight: '500'
    lineHeight: 32px
    letterSpacing: '0'
  body-lg:
    fontFamily: Google Sans
    fontSize: 18px
    fontWeight: '400'
    lineHeight: 28px
    letterSpacing: '0'
  body-md:
    fontFamily: Google Sans
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
    letterSpacing: '0'
  label-md:
    fontFamily: Google Sans
    fontSize: 14px
    fontWeight: '500'
    lineHeight: 20px
    letterSpacing: 0.02em
  label-sm:
    fontFamily: Google Sans
    fontSize: 12px
    fontWeight: '700'
    lineHeight: 16px
    letterSpacing: 0.05em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  unit: 4px
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 40px
  gutter: 24px
  margin: 32px
---

## Brand & Style

This design system is defined by a "Technical Airiness"—a aesthetic that merges high-precision instrumentation with a light, ethereal interface. The brand personality is clinical yet inviting, utilizing **Glassmorphism** to create a sense of depth without weight. 

The system focuses on transparency and clarity, using blurred layers to organize information hierarchies. It targets professional environments that require high data density but want to avoid the visual "heaviness" of traditional enterprise software. The emotional response is one of calm, focused efficiency, where the UI feels like a high-end optical instrument.

## Colors

The palette is anchored in a pristine white and off-white foundation to maximize the "airy" feel. Navigation and structural backgrounds utilize `#F9F9F9`, while the main canvas remains pure `#FFFFFF`. 

Functional accents are strictly divided into two technical categories:
- **In-person:** Utilizes a high-visibility Neon Green and Yellow spectrum. These colors should be used for status indicators, badges, and primary actions related to physical presence.
- **Virtual:** Utilizes a vibrant Neon Pink and Electric Blue spectrum. These colors signify digital-first interactions and remote connectivity.

Neutral tones are kept to a minimum, using alpha-transparent blacks and whites rather than solid greys to maintain the glass-like integrity of the system.

## Typography

This design system exclusively employs **Google Sans** to leverage its geometric precision and modern readability. The typographic scale is generous, prioritizing whitespace around text blocks to prevent visual clutter. 

Labels and technical data points should utilize the `label-sm` style with increased letter spacing and uppercase casing to evoke a "technical readout" aesthetic. Body text maintains a comfortable line-height for long-form readability against semi-transparent backgrounds.

## Layout & Spacing

The layout philosophy follows a **Fluid Grid** model with a strictly enforced 4px baseline. Components are spaced using increments of 8px to ensure a rhythmic, predictable flow. 

To maintain the "airy" feel, the system mandates large internal padding within glass containers (minimum 24px) to prevent content from feeling compressed against the borders. Content should be grouped into distinct modules with significant `xl` (40px) gaps between sections to allow the background blurs to remain visible and effective.

## Elevation & Depth

Hierarchy in this design system is achieved through **Glassmorphism** rather than traditional drop shadows. Depth is communicated via the intensity of backdrop-filters and layer stacking:

1.  **Level 0 (Base):** Solid `#FFFFFF` or `#F9F9F9`.
2.  **Level 1 (Default Container):** Background `rgba(255, 255, 255, 0.4)` with `backdrop-filter: blur(20px)`.
3.  **Level 2 (Floating/Active):** Background `rgba(255, 255, 255, 0.7)` with `backdrop-filter: blur(40px)`.

Every glass container must feature a 1px "Specular Edge"—a border using a subtle white-to-transparent gradient (`linear-gradient(135deg, rgba(255,255,255,0.5), rgba(255,255,255,0.1))`)—to simulate the edge of a physical glass pane.

## Shapes

The shape language balances technical precision with modern softness. The `rounded-md` (0.5rem) setting is used for most standard components like input fields and buttons. Larger containers and cards use `rounded-xl` (1.5rem) to create a friendly, "held" appearance for the glass panes. Interactive elements should never be fully sharp, as the roundedness helps the blurred background feel contained and intentional.

## Components

### Buttons
Primary buttons use a solid white fill with a very subtle inner shadow to look "etched." Accents for "In-person" or "Virtual" are applied via a 2px bottom-border or a glowing hover state. Secondary buttons are ghost-style with the "Specular Edge" border.

### Chips & Badges
Chips are the primary vehicle for the neon accent colors. They use a semi-transparent version of the accent color (15% opacity) for the background and the full-strength neon color for the text and a 2px left-side indicator stripe.

### Cards & Containers
All cards must implement the `backdrop-filter: blur(20px)` and semi-transparent white background. Headlines within cards should have a slightly heavier weight to stand out against the filtered background.

### Input Fields
Inputs are rendered as simple underlines or very light glass containers. On focus, the border transitions from light gray to the relevant accent color (Green/Yellow for physical inputs, Pink/Blue for virtual inputs) with a soft outer glow.

### Lists
Lists are separated by thin, 1px lines using `rgba(0, 0, 0, 0.05)`. Hovering over a list item should trigger a slight increase in the background opacity of that row, enhancing the glass effect locally.