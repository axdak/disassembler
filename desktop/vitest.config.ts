import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // Main-process tests need the Node runtime. Task 7 will add a renderer
    // project with jsdom environment and register `src/renderer/src/test/setup.ts`.
    environment: 'node',
    include: ['src/main/**/*.test.ts'],
  },
})
