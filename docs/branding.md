# Circuit Relay identity

Selected palette: **Coral + Bone**. Three relay inputs join the IGM circuit lettermark; the third dot and its trace remain at 24% opacity to suggest expandability. This is a static brand detail, not a live indicator of enabled engines or occupancy.

## Assets

| Asset | Use |
|---|---|
| [logo-dark.svg](assets/logo-dark.svg) | Transparent wordmark for dark surfaces: Coral `#EF8D76`, Bone `#F0E9DF` |
| [logo-light.svg](assets/logo-light.svg) | Transparent wordmark for light surfaces: dark ink `#303039`, darker coral `#B24A35` |
| [logo-mono.svg](assets/logo-mono.svg) | Single-color wordmark; `currentColor`, black by default as a standalone image |
| [favicon.svg](../gateway/static/favicon.svg) | Compact relay/I icon on dark backing; shipped in the gateway package |
| [social-preview.svg](assets/social-preview.svg) | Editable 1280 × 640 social-preview source |
| [social-preview.png](assets/social-preview.png) | Raster social-preview export for GitHub |

Preserve the third input opacity and aspect ratio. Use the compact icon rather than shrinking the full lettermark to favicon size. The artwork is covered by the repository's MIT license.

## GitHub placement

The README uses dark/light logo variants via a `picture` element. The gateway HTML references its compact favicon. These are normal versioned repository assets.

To configure GitHub's separate custom social preview, upload `social-preview.png` through **Repository → Settings → General → Social preview → Edit → Upload an image** using an authorized repository account. Committing the image alone does not configure that setting. A repository logo does not change the owner's profile avatar. Existing installed gateways need an ordinary package update to receive the favicon; this documentation/branding publication does not update host services.
