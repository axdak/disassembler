// Vitest renderer test setup.
//
// Registers @testing-library/jest-dom DOM matchers (e.g. `toBeInTheDocument`,
// `toBeDisabled`) on Vitest's `expect`, and pulls in @testing-library/react,
// whose module-level side-effect auto-registers `afterEach(cleanup)` so each
// test's mounted tree is unmounted between cases.
//
// This file is listed under `test.setupFiles` in vitest.config.ts and so runs
// for both renderer (jsdom) and main-process (node) tests; the imports here
// are safe to load in either environment — matchers just extend `expect`, and
// the cleanup registration is a no-op for tests that never mount a component.
import '@testing-library/jest-dom/vitest'
import '@testing-library/react'
