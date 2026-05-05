import { defineConfig, devices } from '@playwright/test'

const PORT = 5175
const BASE_URL = `http://localhost:${PORT}`

// Specs scoped to specific projects.
//
// MOBILE_ONLY: hardcodes a small viewport and asserts PWA-style behaviour;
//   running them on desktop projects just adds noise.
// DESKTOP_ONLY: assumes the sidebar is always visible, the session id badge
//   is rendered, etc. — these are layout assumptions that the mobile UI
//   deliberately violates (sidebar hidden behind hamburger, session id
//   hidden to save space). Mobile-specific equivalents live in
//   responsive-mobile.spec.ts.
//
// Specs not in either list (chat-stream, error-resilience, user-timezone)
// run on EVERY project so we catch engine-specific regressions.
const MOBILE_ONLY = ['**/ios-viewport.spec.ts', '**/responsive-mobile.spec.ts']
const DESKTOP_ONLY = [
  '**/background-stream.spec.ts',
  '**/conversation-menu.spec.ts',
  '**/conversation-search.spec.ts',
  '**/conversations-list.spec.ts',
  '**/health-panel.spec.ts',
  '**/initial-load.spec.ts',
]

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI
    ? [['line'], ['html', { open: 'never' }]]
    : 'list',

  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
  },

  // Four projects: desktop chromium + desktop webkit (real Safari engine, the
  // only place Chromium-only assumptions get caught), mobile safari (iPhone
  // 15 Pro device descriptor — closest to the user's PWA target), mobile
  // chrome (Pixel 7 — Android regression).
  projects: [
    {
      name: 'desktop-chromium',
      use: { ...devices['Desktop Chrome'] },
      testIgnore: MOBILE_ONLY,
    },
    {
      name: 'desktop-webkit',
      use: { ...devices['Desktop Safari'] },
      testIgnore: MOBILE_ONLY,
    },
    {
      name: 'mobile-safari',
      use: { ...devices['iPhone 15 Pro'] },
      testIgnore: DESKTOP_ONLY,
    },
    {
      name: 'mobile-chrome',
      use: { ...devices['Pixel 7'] },
      testIgnore: DESKTOP_ONLY,
    },
  ],

  webServer: {
    // Vite will refuse to proxy requests that aren't mocked by `page.route`
    // in tests (unreachable target => ECONNREFUSED). That's the point —
    // every test must mock the endpoints it exercises.
    command: `VITE_API_PROXY_TARGET=http://127.0.0.1:1 npx vite --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    stdout: 'ignore',
    stderr: 'pipe',
    timeout: 30_000,
  },
})
