import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // Main-process tests need the Node runtime. Renderer tests opt into jsdom
    // per-file via a `// @vitest-environment jsdom` docblock (see
    // src/renderer/src/components/workbench.test.tsx). The shared setup file
    // registers @testing-library/jest-dom matchers + auto cleanup and is a
    // no-op for Node-only tests.
    environment: 'node',
    include: ['src/main/**/*.test.ts', 'src/renderer/src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/renderer/src/test/setup.ts'],
  },
})
